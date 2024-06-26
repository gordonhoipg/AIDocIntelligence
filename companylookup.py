import os
import abc
import io
import re
import pandas as pd
import requests

from fuzzywuzzy import process
from fuzzywuzzy import fuzz

    # "valueAddress": {
    #   "houseNumber": "5454",
    #   "road": "BEETHOVEN STREET",
    #   "postalCode": "90066",
    #   "city": "LOS ANGELES",
    #   "state": "CA",
    #   "countryRegion": "USA",
    #   "streetAddress": "5454 BEETHOVEN STREET"
    # }

# TODO: currently each strategy loops through the whole dataframe
# at current volume that is acceptable but consider other alernatives
class MatchStrategy(metaclass=abc.ABCMeta):
    @abc.abstractmethod
    def execute(self, df: pd.DataFrame, invoice_data_dict: dict) -> list:
        return
    
    @abc.abstractmethod
    def dict_has_required_fields(self, invoice_data_dict: dict) -> bool:
        return
    
    def safe_string(self, input: str) -> str:
        return input.replace("\n","").replace("\r","").replace("\t","").strip()
    
class ExternalCompanyNameLookup_MatchStrategy(MatchStrategy):
    def dict_has_required_fields(self, invoice_data_dict: dict) -> bool:
        return invoice_data_dict.get('CustomerName') or None
    
    def execute(self, df: pd.DataFrame, invoice_data_dict: dict) -> list:
        matches = []

        url = "https://postman-echo.com/post"
        payload = {"customer_name": self.safe_string(invoice_data_dict.get('CustomerName').get('valueString'))}

        # Send the HTTP POST request
        response = requests.post(url, data=payload)

        # Check if the request was successful
        if response.status_code == 200:
            # Parse the JSON response
            data = response.json()

            # assuming you got back an array you can pass it
            # back to the orchestrator
            for result in data:
                matches.append({'company_code': result['code'], 'company_name': result['name']})

            return(matches)
        else:
            print(f"Request failed with status code {response.status_code}")
            return(matches)

class FuzzyCompanyName_PostCode_City_RefineByStreetAndHouse_MatchStrategy(MatchStrategy):
    
    def dict_has_required_fields(self, invoice_data_dict: dict) -> bool:
        customer_name = invoice_data_dict.get('CustomerName') or None
        customer_address = invoice_data_dict.get('CustomerAddress') or None
        address_components = None if customer_address is None else customer_address.get('valueAddress')

        return (
            customer_name
            and customer_address
            and address_components
            and customer_name.get("valueString") and customer_name.get('confidence') > 0.8
            and invoice_data_dict.get('CustomerAddress').get('confidence') > 0.8 )
    
    def fuzzy_search_combined(self,query, df, threshold=90, alternative=60 ,limit=10):
        matches = process.extract(query, df['Combined'], limit=limit, scorer=fuzz.token_sort_ratio)
        results = [df.iloc[match[2]] for match in matches if match[1] >= threshold ] 
        alternative_results = [df.iloc[match[2]] for match in matches if match[1] >= alternative and  match[1] < threshold] 
        return results, alternative_results

    def refine_results(self,initial_results, address_queries, threshold=80):
        for column, query in zip(address_queries.keys(), address_queries.values()):
            refined_results = [record for record in initial_results if fuzz.token_set_ratio(record[column], query) >= threshold]
        return refined_results

    def append_final_results_to_matches(self,result,final_results):
        for record in result:
            final_results.append({'company_code': record['Code'], 'company_name': record['Name']})
        return final_results

    def combine_name_address(self,row):
        name_parts = [row['Name'], row['Name 1'], row['Name 2'], row['Postal Code'], row['City']]
        combined = self.unique_words(' '.join(filter(None, name_parts)))        
        return combined
    
    def unique_words(self,string):
        seen = set()
        unique_words = [word for word in string.split() if not (word in seen or seen.add(word))]
        return ' '.join(unique_words)
        
    def execute(self, df: pd.DataFrame, invoice_data_dict: dict) -> list:
        matches = []

        company_name = invoice_data_dict.get('CustomerName').get('valueString')
        address_components = invoice_data_dict.get('CustomerAddress').get('valueAddress')
        
        # Create a combined column for initial search
        df['Combined'] = df.apply(lambda x: self.combine_name_address(x), axis=1)
        #Query the column by company name, postal Code and City
        initial_query = ' '.join(filter(None,[ company_name.casefold(),(address_components.get('postalCode') or '').casefold(),(address_components.get('city') or '').casefold()]))
        initial_query = self.unique_words(initial_query)       

        #Get the Initial Search resuult
        best_results, alternative_results = self.fuzzy_search_combined(initial_query, df)

        #Store the Best Result
        matches = self.append_final_results_to_matches(best_results,matches)

        refine_query = ' '.join(filter(None,[(address_components.get('house') or '').casefold(),(address_components.get('streetAddress') or '').casefold()]))
        refine_query = self.unique_words(refine_query)    

        #Define the refine search components
        refine_components = {
            'Street': refine_query
        }

        #Refine the Initial alternative Result 
        refine_results = self.refine_results(alternative_results, refine_components)

        #Append the refined result to matches 
        matches = self.append_final_results_to_matches(refine_results,matches)

        return matches

class FuzzyCompanyName_FuzzyStreet_ExactCity_ExactPostal_MatchStrategy(MatchStrategy):
    def dict_has_required_fields(self, invoice_data_dict: dict) -> bool:
        customer_name = invoice_data_dict.get('CustomerName') or None
        customer_address = invoice_data_dict.get('CustomerAddress') or None
        address_components = None if customer_address is None else invoice_data_dict.get('CustomerAddress').get('valueAddress')

        return (
            customer_name
            and customer_name.get("valueString") and customer_name.get('confidence') > 0.8
            and customer_address
            and address_components
            and invoice_data_dict.get('CustomerAddress').get('confidence') > 0.8
            and address_components.get('houseNumber')
            and address_components.get('road')
            and address_components.get('city')
            and address_components.get('postalCode'))
    
    def execute(self, df: pd.DataFrame, invoice_data_dict: dict) -> list:
        matches = []

        company_name = self.safe_string(invoice_data_dict.get('CustomerName').get('valueString'))
        address_components = invoice_data_dict.get('CustomerAddress').get('valueAddress')
        for key, val in address_components.items():
            address_components[key] = self.safe_string(val)

        # Iterate over the rows in the DataFrame
        # TODO: is there a better way besides the brute force loop?
        # for company lookup this is probably fine but if more volume is expected than move to a database
        for index, row in df.iterrows():
            # Compare the company name and address with the input
            name_match_ratio = fuzz.ratio(row['Name'].casefold(), company_name.casefold())
            street_match_ratio = fuzz.ratio(row['Street'], address_components.get('houseNumber') + ' ' + address_components.get('road'))
            city_match = address_components.get('city').casefold() == row['City'].casefold()        
            state_match = True # address_components.get('state').casefold() == row['Region'].casefold() # TODO: state abbreviations? non-US addresses?
            postal_match = address_components.get('postalCode') == row['Postal Code'] # TODO: is this US specific??

            # If the match is above a certain threshold, add the company to the list of matches
            # TODO: make the threshold configurable
            # TODO: does this matching logic make sense?
            if name_match_ratio > 80 and street_match_ratio > 80 and city_match and state_match and postal_match:
                matches.append({'company_code': row['Code'], 'company_name': row['Name']})

        return matches

class ExactCompanyName_FuzzyStreet_ExactCity_ExactPostal_MatchStrategy(MatchStrategy):
    def dict_has_required_fields(self, invoice_data_dict: dict) -> bool:
        customer_name = invoice_data_dict.get('CustomerName') or None
        customer_address = invoice_data_dict.get('CustomerAddress') or None
        address_components = None if customer_address is None else invoice_data_dict.get('CustomerAddress').get('valueAddress')

        return (
            customer_name
            and customer_name.get("valueString") and customer_name.get('confidence') > 0.8
            and customer_address
            and address_components
            and invoice_data_dict.get('CustomerAddress').get('confidence') > 0.8
            and address_components.get('houseNumber')
            and address_components.get('road')
            and address_components.get('city')
            and address_components.get('postalCode'))
    
    def execute(self, df: pd.DataFrame, invoice_data_dict: dict) -> list:

        matches = []

        company_name = self.safe_string(invoice_data_dict.get('CustomerName').get('valueString'))
        address_components = invoice_data_dict.get('CustomerAddress').get('valueAddress')
        for key, val in address_components.items():
            address_components[key] = self.safe_string(val)

        # Iterate over the rows in the DataFrame
        # TODO: is there a better way besides the brute force loop?
        # for company lookup this is probably fine but if more volume is expected than move to a database
        for index, row in df.iterrows():
            # Compare the company name and address with the input
            name_match = row['Name'].casefold() == company_name.casefold()
            street_match_ratio = fuzz.ratio(row['Street'], address_components.get('houseNumber') + ' ' + address_components.get('road'))
            city_match = address_components.get('city').casefold() == row['City'].casefold()        
            state_match = True # address_components.get('state').casefold() == row['Region'].casefold() # TODO: state abbreviations? non-US addresses?
            postal_match = address_components.get('postalCode') == row['Postal Code'] # TODO: is this US specific??

            # If the match is above a certain threshold, add the company to the list of matches
            # TODO: make the threshold configurable
            # TODO: does this matching logic make sense?
            if name_match and street_match_ratio > 80 and city_match and state_match and postal_match:
                matches.append({'company_code': row['Code'], 'company_name': row['Name']})

        return matches

class CompanyMatcher():
    strategy: MatchStrategy
    company_listing_df: pd.DataFrame

    def __init__(self, matching_strategy: MatchStrategy, company_listing_df: pd.DataFrame) -> None:
        self.strategy = matching_strategy
        self.company_listing_df = company_listing_df

    def match_companies(self, invoice_data_dict: dict) -> list:        
        return self.strategy.execute(self.company_listing_df, invoice_data_dict)
        