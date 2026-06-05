import json

from dermascan.rules_engine.engine.rules_engine import internet_search


res = internet_search.invoke({"query": "What is the safety of the ingredient parafluorobenzoic acid?", "max_results": 8})

dict_res = json.loads(res)
results = dict_res['results']

for result in results:
    print(result['url'])
    print("-"*100)