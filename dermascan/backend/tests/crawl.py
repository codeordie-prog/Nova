from tavily import TavilyClient

tavily_client = TavilyClient(api_key="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")
response = tavily_client.search(query="whats the price for Anthelios sunscreen?",include_domains=["https://mydawa.com"], instructions="Find the price for Anthelios sunscreen")
#response = tavily_client.crawl("https://docs.tavily.com", instructions="Find all pages on the Python SDK")

for content in response["results"]:
    print(content["content"], end="\n\n")