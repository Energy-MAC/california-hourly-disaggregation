from pathlib import Path
from unicodedata import name
from attr import attributes
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np

ROOT  = Path(__file__).resolve().parents[3]
RAW   = ROOT / 'data' / 'raw'
PROC  = ROOT / 'data' / 'processed' / 'substations'
MISC  = ROOT / 'data' / 'processed' / 'substation_misc'
CHECKS = ROOT / 'data' / 'checks'
def load_substation_load_profiles():
    # Processed interchange (fromba always in CA8)
    print('Loading processed substation load profiles ...')
    loads = pd.read_csv(PROC / 'substation_load_profiles.csv', low_memory=False)
    print(f'  {len(loads):,} load rows')
    # Region TI
    print('Loading attribute data ...')
    attributes = pd.read_csv(PROC / 'substation_attributes.csv')
    print(f'  {len(attributes):,} attribute rows')

    pge_loads = pd.read_csv(RAW / 'pge' / 'pge_layer25_earliest_latest_part001.csv')
    pge_attributes = pd.read_csv(RAW / 'pge' / 'pge_substation_attributes.csv')

    sce_loads = pd.read_csv(RAW / 'sce' / 'sce_combined_raw.csv')
    sce_attributes = pd.read_csv(RAW / 'sce' / 'sce_substation_attributes.csv')
    sce_attributes_alt = pd.read_csv(RAW / 'sce' / 'sce_ica_layer_substations_alt.csv')

    sdge_loads = pd.read_csv(RAW / 'sdge' / 'sdge_substation_profiles_part001.csv')
    sdge_attributes = pd.read_csv(RAW / 'sdge' / 'sdge_substation_attributes.csv')
    sdge_failures = pd.read_csv(RAW / 'sdge' / 'sdge_substation_profiles_failed.csv')
    
    basin = pd.read_csv(MISC / 'ca_substations_2022.csv')
    
    return (loads, attributes), (pge_loads, pge_attributes), (sce_loads, sce_attributes, sce_attributes_alt), (sdge_loads, sdge_attributes,sdge_failures,), (basin,)


UTILITY_NAMES_DICT = {'pge':{'load':'subname','attr':'substation_name'},
                      'sce':{'load':'SUBSTATION','attr':'substation_name','attrAlt':'SUB_NAME'},
                        'sdge':{'load':'AssetName','attr':'substation_name','failure':'substation_name'}}

def compare_loads_and_attributes(loads: pd.DataFrame, attributes: pd.DataFrame, name: str,failures: pd.DataFrame = None, attributesAlt: pd.DataFrame = None) -> None:
    load_name = UTILITY_NAMES_DICT.get(name, {}).get('load')
    attr_name = UTILITY_NAMES_DICT.get(name, {}).get('attr')
    load_subs = set(loads[load_name].str.lower().str.replace(' p.t.',''))
    attr_subs = set(attributes[attr_name].str.lower().str.replace(' p.t.',''))
    only_loads = load_subs - attr_subs
    only_attrs = attr_subs - load_subs
    print(f'{name} substations:')
    print(f'  {len(load_subs):,} in loads')
    print(f'  {len(attr_subs):,} in attributes')
    if failures is not None:
        failure_subs = set(failures[UTILITY_NAMES_DICT.get(name, {}).get('failure')].str.lower().str.replace(' p.t.',''))
        only_loads = only_loads - failure_subs
        only_attrs = only_attrs - failure_subs
        print(f'{name} failures:')
        print(f'  {len(failure_subs):,} in failures')
    if attributesAlt is not None:
        attr_alt_name = UTILITY_NAMES_DICT.get(name, {}).get('attrAlt')
        attr_alt_subs = set(attributesAlt[attr_alt_name].str.lower().str.replace(' p.t.',''))
        only_loads_alt = load_subs - attr_alt_subs
        only_attrs_alt = attr_subs - attr_alt_subs
        only_alt_attrs = attr_alt_subs - attr_subs
        print(f'{name} alt attributes:')
        print(f'  {len(attr_alt_subs):,} in alt attributes')
    print(f'  {len(only_loads):,} only in loads')
    print(f'  {len(only_attrs):,} only in attributes')
    if len(only_loads) > 0:
        loads.loc[loads[load_name].str.lower().str.replace(' p.t.','').isin(only_loads)].to_csv(CHECKS / f'{name}_only_in_loads.csv', index=False)
    if len(only_attrs) > 0:
        attributes.loc[attributes[attr_name].str.lower().str.replace(' p.t.','').isin(only_attrs)].to_csv(CHECKS / f'{name}_only_in_attributes.csv', index=False)
    if attributesAlt is not None:
        if len(only_loads_alt) > 0:
            loads.loc[loads[load_name].str.lower().str.replace(' p.t.','').isin(only_loads_alt)].to_csv(CHECKS / f'{name}_only_in_loads_alt.csv', index=False)
        if len(only_attrs_alt) > 0:
            attributes.loc[attributes[attr_name].str.lower().str.replace(' p.t.','').isin(only_attrs_alt)].to_csv(CHECKS / f'{name}_only_in_attributes_alt.csv', index=False)
        if len(only_alt_attrs) > 0:
            attributesAlt.loc[attributesAlt[attr_alt_name].str.lower().str.replace(' p.t.','').isin(only_alt_attrs)].to_csv(CHECKS / f'{name}_only_in_attributes_alt_only.csv', index=False)
        print(f'  {len(only_loads_alt):,} only in loads vs alt attributes')
        print(f'  {len(only_attrs_alt):,} only in attributes (not alt)')
        print(f'  {len(only_alt_attrs):,} only in alt attributes not the default')

    return

def compare_substation_locations(basin: pd.DataFrame, combined: tuple, pge: tuple,sce:tuple,sdge:tuple) -> None:
    loads,attributes = combined
    for utility in ['sce','pge','sdge']:
        utility_basin = basin.loc[basin['owner_std']==utility].copy(deep=True)
        
        if utility == 'sce':
            sce_loads,sce_attributes,sce_attributes_alt = sce
            basin_subs = set(utility_basin['name'].str.lower().str.replace(' p.t.',''))
            sce_load_subs = set(sce_loads[UTILITY_NAMES_DICT.get(utility, {}).get('load')].str.lower().str.replace(' p.t.',''))
            sce_attr_subs = set(sce_attributes[UTILITY_NAMES_DICT.get(utility, {}).get('attr')].str.lower().str.replace(' p.t.',''))
            sce_attr_alt_subs = set(sce_attributes_alt[UTILITY_NAMES_DICT.get(utility, {}).get('attrAlt')].str.lower().str.replace(' p.t.',''))
            
            only_basin_attr = basin_subs - sce_attr_subs
            only_basin_load = basin_subs - sce_load_subs
            only_basin_attr_alt = basin_subs - sce_attr_alt_subs
            only_attr_basin = sce_attr_subs - basin_subs
            only_load_basin = sce_load_subs - basin_subs
            only_attr_alt_basin = sce_attr_alt_subs - basin_subs
            print(f'{utility} lat/lon:')
            print(f'  {len(basin_subs):,} in basin not in attributes')
            print(f'  {len(sce_load_subs):,} in loads not in basin')
            print(f'  {len(sce_attr_subs):,} in attributes not in basin')
            print(f'  {len(sce_attr_alt_subs):,} in alt attributes not in basin')
            print(f'  {len(only_basin_attr):,} only in basin not in attributes')
            print(f'  {len(only_basin_load):,} only in basin (not loads)')
            print(f'  {len(only_basin_attr_alt):,} only in basin (not alt attributes)')
            print(f'  {len(only_attr_basin):,} only in attributes')
            print(f'  {len(only_load_basin):,} only in loads')
            print(f'  {len(only_attr_alt_basin):,} only in alt attributes')

            intersection = basin_subs & sce_attr_subs & sce_load_subs
            intersection_alt = basin_subs & sce_attr_alt_subs & sce_load_subs
            print(f'  {len(intersection):,} in basin, attributes, and loads')
            print(f'  {len(intersection_alt):,} in basin, alt attributes, and loads')
            basin_intersect = utility_basin.loc[utility_basin['name'].str.lower().str.replace(' p.t.','').isin(intersection)].copy(deep=True)
            basin_intersect_alt = utility_basin.loc[utility_basin['name'].str.lower().str.replace(' p.t.','').isin(intersection_alt)].copy(deep=True)
            print(f'    of which {len(basin_intersect):,} have lat/lon in basin')
            print(f'    of which {len(basin_intersect_alt):,} have lat/lon in basin')
            attributes_intersect = attributes.loc[attributes['substation_name'].str.lower().str.replace(' p.t.','').isin(intersection)].copy(deep=True)
            attributes_intersect_alt = attributes.loc[attributes['substation_name'].str.lower().str.replace(' p.t.','').isin(intersection_alt)].copy(deep=True)
            print(f'    of which {len(attributes_intersect):,} have lat/lon in attributes')
            print(f'    of which {len(attributes_intersect_alt):,} have lat/lon in alt attributes')
            merged = pd.merge(basin_intersect, attributes_intersect, left_on='name', right_on='substation_name', how='inner',suffixes = ['_basin','_attr'])
            merged_alt = pd.merge(basin_intersect, attributes_intersect_alt, left_on='name', right_on='substation_name', how='inner',suffixes = ['_basin','_attr'])
            merged['lat_diff'] = merged['latitude_basin'] - merged['latitude_attr']
            merged_alt['lat_diff'] = merged_alt['latitude_basin'] - merged_alt['latitude_attr']
            merged['long_diff'] = merged['longitude_basin'] - merged['longitude_attr']
            merged_alt['long_diff'] = merged_alt['longitude_basin'] - merged_alt['longitude_attr']
            import pdb;pdb.set_trace()
        # else:
        #     _,utility_attributes = pge if utility == 'pge' else sdge
        #     utility_attributes['latlon'] = utility_attributes['latitude'].round(3).astype(str) + ',' + utility_attributes['longitude'].round(3).astype(str)
        #     basin_latlons = set(utility_basin['latlon'])
        #     attr_latlons = set(utility_attributes['latlon'])
        #     only_basin_attr = basin_latlons - attr_latlons
        #     only_attr_basin = attr_latlons - basin_latlons
        #     print(f'{utility} lat/lon:')
        #     print(f'  {len(basin_latlons):,} in basin')
        #     print(f'  {len(attr_latlons):,} in attributes')
        #     print(f'  {len(only_basin_attr):,} only in basin')
        #     print(f'  {len(only_attr_basin):,} only in attributes')


def main():
    combined,pge,sce,sdge, basin = load_substation_load_profiles()
    loads, attributes = combined
    pge_loads, pge_attributes = pge
    sce_loads, sce_attributes, sce_attributes_alt = sce
    sdge_loads, sdge_attributes,sdge_failures = sdge
    basin,  = basin

    # compare_loads_and_attributes(pge_loads, pge_attributes, 'pge')
    # compare_loads_and_attributes(sce_loads, sce_attributes, 'sce', attributesAlt=sce_attributes_alt)
    # compare_loads_and_attributes(sdge_loads, sdge_attributes, 'sdge',failures = sdge_failures)
    compare_substation_locations(basin, combined,pge,sce,sdge)

    return


if __name__ == '__main__':
    main()