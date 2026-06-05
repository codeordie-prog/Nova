"""Concrete Rules Engine implementation."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import psycopg2
import redis
from dotenv import load_dotenv

from dermascan.rules_engine.engine.engine import (
    Engine,
    IngredientIndexResult,
    RulesEngineRequest,
    RulesEngineResponse,
)
from dermascan.rules_engine.knowledge_agent.knowledge_agent import (
    create_knowledge_agent,
    init_model_with_tools,
    internet_search,
)
from dermascan.rules_engine.knowledge_agent.prompt import system_prompt
from dermascan.rules_engine.vector_store.build_vectorstore import VectorStore
from dermascan.shared.logger import get_logger

# Load environment variables
load_dotenv()

# Initialize logger
logger = get_logger(__name__)


class RulesEngine(Engine):
    """Routes and assesses product, skin, and hybrid reviews."""

    def __init__(
        self,
        *,
        redis_client: redis.Redis | None = None,
        vector_store: VectorStore | None = None,
    ):
        # Initialize redis connection
        self.redis_url = os.getenv("REDIS_URL", "")
        if redis_client is not None:
            self.redis = redis_client
        elif self.redis_url:
            self.redis = redis.Redis.from_url(self.redis_url, decode_responses=True)
        else:
            raise RuntimeError("REDIS_URL is required for RulesEngine")

        # Initialize cache prefix for indexed cosmetic ingredients
        self.cache_prefix = os.getenv(
            "RULES_ENGINE_INGREDIENT_CACHE_PREFIX",
            "rules_engine:indexed",
        )

        # Initialize vector store
        self.vector_store = vector_store or VectorStore()

        # Initialize openai api key
        self.openai_api_key = str(os.environ.get("OPENAI_API_KEY_QUTIS", ""))

        # Initialize knowledge agent
        self.agent = create_knowledge_agent(
            model=init_model_with_tools(api_key=self.openai_api_key),
            system_prompt=system_prompt,
            api_key=self.openai_api_key,
        )

        # Get postgres variables
        self.postgres_host = os.environ.get("POSTGRES_HOST", "localhost")
        self.postgres_port = os.environ.get("POSTGRES_PORT", "5432")
        self.postgres_database = os.environ.get("POSTGRES_DATABASE", "postgres")
        self.postgres_user = os.environ.get("POSTGRES_USER", "postgres")
        self.postgres_password = os.environ.get("POSTGRESS_PASSWORD", "")
        self.postgres_table = os.environ.get("POSTGRES_TABLE", "cir_pdfs")

    @staticmethod
    def _normalize_ingredient(name: str) -> str:
        """
        Normalize ingredient name - in order to match the one on the database.
        Its a static method, meaning it can be accessed without initializing a Rules Engine instance.


        Args:
            name (str): Name of the cosmetic ingredient

        Returns:
            str: Normalized name
        """
        return "".join(ch.lower() for ch in str(name or "") if ch.isalnum())

    def _cache_key(self, normalized_name: str) -> str:
        """
        Create a Redis cache key for the Cosmetic ingredient.

        This is useful when caching indexed ingredients inorder to avoid redudancy.

        Args:
            normalized_name (str): Ingredient normalized name

        Returns:
            str: Redis cache key
        """
        return f"{self.cache_prefix}:{normalized_name}"

    def _is_cached(self, normalized_name: str) -> bool:
        """
        Check whether a given ingredient name is already indexed by confirming
        whether the cache key exists.

        Args:
            normalized_name (str): Ingredient normalized name

        Returns:
            bool: True if the ingredient is cached, False otherwise
        """
        return bool(normalized_name) and bool(
            self.redis.exists(self._cache_key(normalized_name))
        )

    def _mark_cached(self, normalized_name: str, ingredient_id: str) -> None:
        """
        Mark an ingredient as cached in Redis.
        
        Args:
            normalized_name (str): Ingredient normalized name
            ingredient_id (str): Ingredient ID
        """
        self.redis.hset(
            self._cache_key(normalized_name),
            mapping={
                "normalized_name": normalized_name,
                "ingredient_id": ingredient_id,
                "indexed": "true",
            },
        )

    def _connect_postgres(self):
        """
        Establish a postgres connection

        """
        if not self.postgres_password:
            raise ValueError("POSTGRESS_PASSWORD environment variable is required")

        return psycopg2.connect(
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_database,
            user=self.postgres_user,
            password=self.postgres_password,
        )

    def _fetch_report_rows(
        self,
        ingredient_names: list[str],
    ) -> dict[str, dict[str, str]]:
        """
        Fetch CIR report rows from the database by normalized ingredient names.

        Args:
            ingredient_names (list[str]): List of ingredient names to fetch
            
        Returns:
            dict[str, dict[str, str]]: Dictionary mapping normalized names to their report data
        """
        # Return empty dictionary if the ingredient names are not provided
        if not ingredient_names:
            return {}

        # Create normalized ingredient names
        requested = [
            self._normalize_ingredient(name)
            for name in ingredient_names
            if str(name).strip()
        ]
        requested = [item for item in requested if item]
        if not requested:
            return {}

        table_name = self.postgres_table
        if not table_name.replace("_", "").isalnum():
            raise ValueError(f"Invalid table name: {table_name}")

        # Create connection to postgres and execute a fetch command for the rows
        conn = self._connect_postgres()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = %s
                """,
                (table_name,),
            )
            existing_columns = {row[0] for row in cur.fetchall()}

            def pick_col(*candidates: str) -> str | None:
                for candidate in candidates:
                    if candidate in existing_columns:
                        return candidate
                return None

            def col_expr(*candidates: str, fallback: str = "''") -> str:
                selected = pick_col(*candidates)
                return f'"{selected}"' if selected else fallback

            ingredient_id_expr = col_expr("ingredient_id", "Ingredient_ID", fallback="''")
            ingredient_name_expr = col_expr(
                "cir_ingredient_name",
                "CIR_Ingredient_Name",
                fallback="''",
            )
            function_expr = col_expr("function", "Function", fallback="'unknown'")
            report_url_expr = col_expr("report_url", "Report_URL", fallback="''")
            attachment_url_expr = col_expr("attachment_url", "Attachment_URL", fallback="''")
            storage_path_expr = col_expr("storage_path", "Storage_Path", fallback="''")
            download_url_expr = col_expr("download_url", "Download_URL", fallback="''")
            normalized_names_expr = col_expr(
                "normalized_names",
                "Normalized_Names",
                fallback="''",
            )
            pdf_name_expr = col_expr("pdf_name", "PDF_Name", fallback="''")

            match_source_expr = (
                f"COALESCE({ingredient_name_expr}, {normalized_names_expr}, {ingredient_id_expr}, '')"
            )
            ingredient_match_expr = (
                f"regexp_replace(lower({match_source_expr}), '[^a-z0-9]+', '', 'g')"
            )

            cur.execute(
                f'''
                SELECT
                    COALESCE({ingredient_id_expr}, '') AS ingredient_id,
                    COALESCE({ingredient_name_expr}, '') AS cir_ingredient_name,
                    COALESCE({function_expr}, 'unknown') AS function,
                    COALESCE({report_url_expr}, '') AS report_url,
                    COALESCE({attachment_url_expr}, '') AS attachment_url,
                    COALESCE({storage_path_expr}, '') AS storage_path,
                    COALESCE({download_url_expr}, '') AS download_url,
                    COALESCE({normalized_names_expr}, '') AS normalized_names,
                    COALESCE({pdf_name_expr}, '') AS pdf_name
                FROM "{table_name}"
                WHERE COALESCE({download_url_expr}, '') <> ''
                AND {ingredient_match_expr} = ANY(%s)
                ''',
                (requested,),
            )

            rows_by_normalized_name: dict[str, dict[str, str]] = {}
            for row in cur.fetchall():
                ingredient_name = str(row[1] or "")
                normalized_names = str(row[7] or "")
                candidates = {self._normalize_ingredient(ingredient_name)}
                candidates.update(
                    self._normalize_ingredient(part)
                    for part in re.split(r"[|,;\s]+", normalized_names)
                    if part.strip()
                )

                payload = {
                    "ingredient_id": str(row[0] or ""),
                    "cir_ingredient_name": ingredient_name,
                    "function": str(row[2] or "unknown"),
                    "report_url": str(row[3] or ""),
                    "attachment_url": str(row[4] or ""),
                    "storage_path": str(row[5] or ""),
                    "download_url": str(row[6] or ""),
                    "pdf_name": str(row[8] or ""),
                }

                for candidate in candidates:
                    if candidate and candidate in requested and candidate not in rows_by_normalized_name:
                        rows_by_normalized_name[candidate] = payload

            return rows_by_normalized_name
        finally:
            cur.close()
            conn.close()

    def ensure_reports_exists(
        self,
        ingredients: list[str],
    ) -> list[IngredientIndexResult]:
        """
        Ensure safety and toxicological reports for the ingredients exists.
        If not yet indexed and reports exists, the ingredient is immedieately indexed.
        Args:
            ingredients (list[str]): List of ingredient names to check
            
        Returns:
            list[IngredientIndexResult]: List of ingredient index results
        """
        report_rows = self._fetch_report_rows(ingredients)
        results: list[IngredientIndexResult] = []

        for ingredient in ingredients:

            # If not normalized - possibly empty mark False and continue
            normalized = self._normalize_ingredient(ingredient)
            if not normalized:
                results.append(
                    IngredientIndexResult(
                        ingredient=ingredient,
                        normalized_name="",
                        indexed=False,
                        source="missing",
                        notes="empty ingredient name",
                    )
                )
                continue
            
            # If already indexed mark True and proceed
            if self._is_cached(normalized):
                results.append(
                    IngredientIndexResult(
                        ingredient=ingredient,
                        normalized_name=normalized,
                        indexed=True,
                        source="redis_cache",
                    )
                )
                continue
            
            # If report not available, Mark False and proceed
            row = report_rows.get(normalized)
            if not row:
                results.append(
                    IngredientIndexResult(
                        ingredient=ingredient,
                        normalized_name=normalized,
                        indexed=False,
                        source="missing",
                        notes="report not found in Postgres",
                    )
                )
                continue

            try:
                # If not indexed, download report, index and mark True
                ingredient_id = row["ingredient_id"] or normalized
                self.vector_store.index_pdf_from_firebase(
                    pdf_url=row["download_url"],
                    ingredient_id=ingredient_id,
                    cir_ingredient_name=row["cir_ingredient_name"] or ingredient,
                    function=row["function"] or "unknown",
                    report_url=row["report_url"],
                    attachment_url=row["attachment_url"] or row["download_url"],
                    storage_path=row["storage_path"] or row["download_url"],
                )
                self._mark_cached(normalized, ingredient_id=ingredient_id)
                results.append(
                    IngredientIndexResult(
                        ingredient=ingredient,
                        normalized_name=normalized,
                        ingredient_id=ingredient_id,
                        indexed=True,
                        source="indexed_now",
                    )
                )
            except Exception as exc:
                logger.exception("failed indexing ingredient=%s", ingredient)
                results.append(
                    IngredientIndexResult(
                        ingredient=ingredient,
                        normalized_name=normalized,
                        ingredient_id=row.get("ingredient_id", ""),
                        indexed=False,
                        source="index_failed",
                        notes=str(exc),
                    )
                )

        return results

    @staticmethod
    def _serialize_matches(matches: list[Any]) -> list[dict[str, Any]]:
        """
        Serialize matches to a list of dictionaries

        Args:
            matches: List of match objects from the vector store

        Returns:
            List of dictionaries containing match information
        """
        serialized: list[dict[str, Any]] = []
        for match in matches:
            metadata = getattr(match, "metadata", {}) or {}
            serialized.append(
                {
                    "score": float(getattr(match, "score", 0.0)),
                    "ingredient_id": str(metadata.get("ingredient_id", "")),
                    "ingredient_name": str(metadata.get("cir_ingredient_name", "")),
                    "page": metadata.get("page", ""),
                    "chunk_text": str(metadata.get("chunk_text", "")),
                    "report_url": str(metadata.get("report_url", "")),
                }
            )
        return serialized

    def evaluate_safety(self, ingredient_ids: list[str]) -> list[dict[str, Any]]:
        """
        Evaluate safety of the ingredients by Querying the Toxicological reports.

        Its important to consider the Query text, since a good query text will help to get more relevant results.
        Read the Search Policy in `search.md` file to understand how the Algorithm works.
        Args:
            ingredient_ids: List of ingredient IDs to evaluate
            
        Returns:
            List of dictionaries containing safety evaluation results
        """
        if not ingredient_ids:
            return []

        matches = self.vector_store.query_safety_report(
            query_text="safety assessment toxicology irritation sensitization mutagenicity carcinogenicity reproductive toxicity dermal exposure concentration of use NOAEL adverse effects CIR conclusion.",
            ingredient_ids=ingredient_ids,
            top_k=12,
            score_threshold=0.4,
        )
        return self._serialize_matches(matches)

    def evaluate_contraindications(self, ingredient_ids: list[str]) -> list[dict[str, Any]]:
        """
        Evaluate contraindications of the ingredients by Querying the Cross-Ingredient Interactions.
        Args:
            ingredient_ids: List of ingredient IDs to evaluate
            
        Returns:
            List of dictionaries containing contraindications evaluation results
        """
        if not ingredient_ids:
            return []

        matches = self.vector_store.query_cross_ingredient_interactions(
            query_text="Find contraindications, interactions, and incompatibility evidence.",
            ingredient_ids=ingredient_ids,
            top_k=12,
            score_threshold=0.4,
        )
        return self._serialize_matches(matches)

    def evaluate_irritation_and_dermatology(
        self,
        ingredient_ids: list[str],
    ) -> list[dict[str, Any]]:
        """
        Evaluate irritation and dermatology of the ingredients by Querying the Irritation and Dermatology Safety Reports.
        Args:
            ingredient_ids: List of ingredient IDs to evaluate
            
        Returns:
            List of dictionaries containing irritation and dermatology evaluation results
        """
        if not ingredient_ids:
            return []

        matches = self.vector_store.query_irritation_and_dermatology(
            query_text="Find irritation, sensitization, and dermatology safety evidence.",
            ingredient_ids=ingredient_ids,
            top_k=12,
            score_threshold=0.1,
        )
        return self._serialize_matches(matches)

    def assess_product_review(self, request: RulesEngineRequest) -> RulesEngineResponse:
        """
        Assess product review

        Args:
            request (RulesEngineRequest): request body.

        Returns:
            RulesEngineResponse : structured response emitted to the signal stream.
        
        """

        # Ensure reports exists - index them if not
        indexed_results = self.ensure_reports_exists(request.ingredients)
        ingredient_ids = [
            item.ingredient_id
            for item in indexed_results
            if item.indexed and item.ingredient_id
        ]

        # Create the report
        report: dict[str, Any] = {
            "product_name": request.product_name,
            "description": request.description,
            "ingredients": request.ingredients,
            "safety": self.evaluate_safety(ingredient_ids),
            "contraindications": self.evaluate_contraindications(ingredient_ids),
            "irritation": self.evaluate_irritation_and_dermatology(ingredient_ids),
        }

        summary = (
            f"Reviewed {len(request.ingredients)} ingredient(s). "
            f"Indexed {len(ingredient_ids)} ingredient report(s) for evidence retrieval."
        )

        return RulesEngineResponse(
            request_id=request.request_id,
            case_id=request.case_id,
            client_id=request.client_id,
            message_id=request.message_id,
            channel=request.channel,
            review_type="product",
            summary=summary,
            sources=["https://cir-reports.cir-safety.org/"],
            escalation_notes=request.escalation_notes,
            indexed_ingredients=indexed_results,
            report=report,
        )

    def assess_skin_review(self, request: RulesEngineRequest) -> RulesEngineResponse:
        """
        Assess skin review

        Args:
            request (RulesEngineRequest): request body.

        Returns:
            RulesEngineResponse : structured response emitted to the signal stream.
        
        """
        
        query: str = request.diagnosis if request.diagnosis else ""
        if not query:
            report = {
            "skin_context": request.skin_context.model_dump(),
            "diagnosis": "No skin review report at the moment.",
            "summary": "No summary at the moment",
            "treatment": "No treatment plan at the moment",
            "internet_research": "No research at the moment"
            }

            return  RulesEngineResponse(
            request_id=request.request_id,
            case_id=request.case_id,
            client_id=request.client_id,
            message_id=request.message_id,
            channel=request.channel,
            review_type="skin",
            summary="Unable to perform research when the skin review report is empty",
            sources=[],
            escalation_notes=request.escalation_notes,
            indexed_ingredients=[],
            report=report,
        )

        # Tavily-backed tool returns a *string* (usually JSON), but it can also
        # return plain text error messages when misconfigured / fails.
        research_raw = internet_search.invoke({"query": query, "max_results": 8})
        research_text = research_raw if isinstance(research_raw, str) else str(research_raw)

        def _extract_urls(text: str) -> list[str]:
            """
            Extract URLs from the text

            Args:
                text (str): Text to extract URLs from
            
            Returns:
                list[str]: List of URLs
            """
            # Best-effort URL extraction for cases where JSON parsing fails.
            if not text:
                return []
            # Trim common trailing punctuation from URLs.
            urls = re.findall(r"https?://[^\s\"']+", text)
            cleaned: list[str] = []
            for url in urls:
                url = url.rstrip(").,;]>}").strip()
                if url and url not in cleaned:
                    cleaned.append(url)
            return cleaned

        dict_research: dict[str, Any] = {}
        sources: list[str] = []
        if isinstance(research_raw, dict):
            dict_research = research_raw
        else:
            # research_text is the most common type (internet_search returns JSON via json.dumps()).
            try:
                dict_research = json.loads(research_text)
            except json.JSONDecodeError:
                dict_research = {}

        report = {
            "skin_context": request.skin_context.model_dump(),
            "diagnosis": request.diagnosis,
            "summary": request.summary,
            "treatment": request.treatment,
            "internet_research": research_text,
        }

        # Build sources from Tavily's response shape when present.
        results = dict_research.get("results") if isinstance(dict_research, dict) else None
        if isinstance(results, list):
            for result in results:
                if isinstance(result, dict):
                    url = result.get("url")
                    if isinstance(url, str) and url:
                        sources.append(url)

        # Fallback: extract URLs from the raw research output.
        if not sources:
            sources = _extract_urls(research_text)

        # De-duplicate while keeping original order.
        sources = list(dict.fromkeys(sources))
        return RulesEngineResponse(
            request_id=request.request_id, 
            case_id=request.case_id,
            client_id=request.client_id,
            message_id=request.message_id,
            channel=request.channel,
            review_type="skin",
            summary="Completed skin research synthesis from internet sources.",
            sources=sources,
            escalation_notes=request.escalation_notes,
            indexed_ingredients=[],
            report=report,
        )

    def process(self, request: RulesEngineRequest) -> RulesEngineResponse:
        """
        Process 
        
        """
        if request.review_type.startswith("skin") or request.review_type.startswith("diagnose"):
            return self.assess_skin_review(request)

        if request.review_type.startswith("hybrid"):
            product_result = self.assess_product_review(request)
            skin_result = self.assess_skin_review(request)
            return RulesEngineResponse(
                request_id=request.request_id,
                case_id=request.case_id,
                client_id=request.client_id,
                message_id=request.message_id,
                channel=request.channel,
                review_type="hybrid",
                summary=(
                    f"Product review:\n{product_result.summary}\n\n"
                    f"Skin review:\n{skin_result.summary}"
                ),
                sources=list(dict.fromkeys(product_result.sources + skin_result.sources)),
                escalation_notes=request.escalation_notes,
                indexed_ingredients=product_result.indexed_ingredients,
                report={
                    "product": product_result.report,
                    "skin": skin_result.report,
                },
            )

        return self.assess_product_review(request)

if __name__ == "__main__":

    engine = RulesEngine()

    res = engine._fetch_report_rows(
        ingredient_names=["Triethanolamine"]
    )

    print(res)

    ingredients = [k for k in res]

    print(ingredients)

    ingredient_ids = [res[k]['ingredient_id'] for k in ingredients]

    print(ingredient_ids)

    index_results = engine.ensure_reports_exists(
        ingredients=ingredients
    )

    print(index_results)

    evals = []
    for idx_res in index_results:
        if idx_res.indexed:
            evals.append(idx_res.ingredient_id)

    
    report = engine.evaluate_safety(
        ingredient_ids=evals
    )

    print(report)