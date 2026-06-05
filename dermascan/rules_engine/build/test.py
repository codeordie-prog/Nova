"""
Testing pinecone client
"""

import os
from uuid import uuid4
from pinecone import Pinecone
from dotenv import load_dotenv
from logging import Logger


from langchain_openai.embeddings import OpenAIEmbeddings

from dermascan.shared.logger import get_logger

# Load environment variables
load_dotenv()

# Initialize logger
logger: Logger = get_logger("vector_store")

PINECONE_API_KEY_QUTIS = str(os.environ.get("PINECONE_API_KEY_QUTIS", ""))
OPENAI_API_KEY_QUTIS = str(os.environ.get("OPENAI_API_KEY_QUTIS", ""))
INDEX_NAME = "test-index"


def _build_index(
    index_name: str, 
    cloud: str = "aws", 
    region: str = "us-east-1",
    dimension: int = 3072,
    metric: str = "cosine"
):
    try:
        if not PINECONE_API_KEY_QUTIS:
            raise ValueError("PINECONE API KEY is not set")
        
        if not OPENAI_API_KEY:
            raise ValueError("OPENAI API KEY is not set")

        pc: Pinecone = Pinecone(api_key=PINECONE_API_KEY_QUTIS)
        index_name: str = INDEX_NAME
        
        if not pc.has_index(index_name):

            logger.info(f"Creating index {index_name}....")

            pc.create_index(
                name=index_name,
                dimension=dimension,
                metric=metric,
                spec={
                    "serverless": {
                        "cloud": cloud,
                        "region": region
                    }
                }
            )

            logger.info("Successfully created index")

    except ValueError as err:
        logger.error("Confirm if API Keys are set as required", err)
        raise
    except Exception as e:
        logger.error("An exception occured: ", e)

def _has_index(pinecone_client: Pinecone, index_name: str):
    return pinecone_client.indexes.exists(
        index_name
    )


class VectorStore:
    def __init__(
        self,
        index_name: str,
        pinecone_api_key: str = PINECONE_API_KEY_QUTIS,
    ):

        self.pc = Pinecone(api_key=pinecone_api_key)
        self.index_name = index_name

        if not _has_index(self.pc, self.index_name):
            logger.warning("Index not found, creating one....")
            _build_index(self.index_name)

        logger.info("Index is ready...")    
        self.index = self.pc.Index(self.index_name) 

    def embedd_text(self, text: str):
        embeddings = OpenAIEmbeddings(
            model="text-embedding-3-large",
            dimensions=3072,
            api_key=OPENAI_API_KEY_QUTIS
        )
        
        logger.info("Attempting text embedding...")
        vectors = embeddings.embed_documents([text], 500)

        logger.info("Successfully embedded text.")

        return vectors[0]


    def upsert_text(self, text: str, id: str = None, metadata: dict = None):
        
        logger.info("Attempting upsertion....")
        vector = self.embedd_text(text)

        if not id:
            id = str(uuid4())

        self.index.upsert(
            vectors=[{
                "id": id,
                "values": vector,
                "metadata": {
                    'text':text,
                    **(metadata or {})
                }
            },]
        )

        logger.info(f"Successfully added the text into the index with id: {id}")

        return id
    

    def query(self, query_text: str, top_k: int = 5):
        vector = self.embedd_text(query_text)
        results = self.index.query(
            vector=vector,
            top_k=top_k,
            include_metadata=True,

        )
        return results.matches

    def query_with_score_threshold(
        self, 
        query_text: str, 
        top_k: int = 5, 
        score_threshold: float = 0.7
    ):

        results = self.query(query_text, top_k)
        return [r for r in results if r.score >= score_threshold]
    
    def delete_text(self):
        pass



if __name__ == "__main__":

    vector_store = VectorStore(
        index_name="test-index"
    )

    text = "Niacinamide, also known as nicotinamide, is a form of vitamin B3 widely used in skincare formulations for its ability to support the skin barrier, improve uneven skin tone, and reduce the appearance of fine lines and inflammation. It is generally considered safe and well-tolerated across most skin types, including sensitive skin, with low risk of irritation even at moderate concentrations. In cosmetic formulations, niacinamide is commonly used in concentrations ranging from 2% to 5%, where it has been shown to help regulate sebum production and improve overall skin texture."

    vector_store.upsert_text(text)

    query = "in cosmetic formulations, what is the used concentrations range?"

    output = vector_store.query_with_score_threshold(query, score_threshold=0.15)
    
    for result in output:
        print(result.id)
        if result.metadata:
            print(result.metadata.get('text', 'No text available'))
        print(f"Score: {result.score}")
        print("---")
