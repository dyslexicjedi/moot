"""
Vector database for moot knowledge base using ChromaDB.
Stores discussions and external documents with semantic search.
"""

import os
import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass

import chromadb
from chromadb.config import Settings

logger = logging.getLogger("vector-store")


@dataclass
class SearchResult:
    """A single search result from the vector database."""
    collection: str
    id: str
    text: str
    metadata: Dict
    score: float


class VectorStore:
    """ChromaDB wrapper for moot knowledge base."""
    
    EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
    
    def __init__(self, persist_dir: str):
        self.persist_dir = persist_dir
        self.client = chromadb.PersistentClient(
            path=persist_dir,
            settings=Settings(anonymized_telemetry=False)
        )
        
        self.collections = {
            "moot_archive": self.client.get_or_create_collection(
                name="moot_archive",
                metadata={"description": "Archived moot discussions"}
            ),
            "external_docs": self.client.get_or_create_collection(
                name="external_docs",
                metadata={"description": "External documents and knowledge"}
            ),
            "personal_notes": self.client.get_or_create_collection(
                name="personal_notes",
                metadata={"description": "Personal notes and reminders"}
            )
        }
        
        self._embedding_function = None
        logger.info("Vector store initialized at %s", persist_dir)
    
    def _get_embedding_function(self):
        """Lazy-load the embedding model."""
        if self._embedding_function is None:
            try:
                from sentence_transformers import SentenceTransformer
                logger.info("Loading embedding model: %s", self.EMBEDDING_MODEL_NAME)
                self._embedding_function = SentenceTransformer(self.EMBEDDING_MODEL_NAME)
                logger.info("Embedding model loaded successfully")
            except ImportError:
                logger.error("sentence-transformers not installed. Run: pip install sentence-transformers")
                raise
            except Exception as e:
                logger.error("Failed to load embedding model: %s", e)
                raise
        return self._embedding_function
    
    def _generate_embedding(self, text: str) -> List[float]:
        """Generate embedding using sentence-transformers."""
        model = self._get_embedding_function()
        embedding = model.encode(text, convert_to_numpy=True)
        return embedding.tolist()
    
    async def archive_moot(self, moot_id: str, topic: str, 
                          discussion_text: str, participants: List[str],
                          history: List[Dict]):
        """Store a completed moot in the archive.
        
        Stores both:
        1. Full moot as a single document
        2. Individual chunks per speaker for fine-grained search
        """
        from council import export_discussion_text, split_by_speaker
        
        # Store full moot
        full_text = await export_discussion_text(history)
        self.collections["moot_archive"].add(
            ids=[moot_id],
            embeddings=[self._generate_embedding(full_text)],
            documents=[full_text],
            metadatas=[{
                "type": "full_moot",
                "topic": topic,
                "participants": ",".join(participants),
                "timestamp": datetime.now().isoformat(),
                "chunk_count": len(history)
            }]
        )
        
        # Store individual speaker chunks
        chunks = split_by_speaker(history)
        for i, (chunk_text, entry) in enumerate(chunks):
            chunk_id = f"{moot_id}_chunk_{i}"
            self.collections["moot_archive"].add(
                ids=[chunk_id],
                embeddings=[self._generate_embedding(chunk_text)],
                documents=[chunk_text],
                metadatas=[{
                    "type": "speaker_chunk",
                    "speaker": entry["speaker"],
                    "topic": topic,
                    "moot_id": moot_id,
                    "timestamp": datetime.now().isoformat()
                }]
            )
        
        logger.info("Archived moot %s with %d chunks", moot_id, len(chunks) + 1)
    
    async def add_document(self, doc_id: str, source: str, 
                          content: str, metadata: Dict = None) -> int:
        """Add an external document to the knowledge base.
        
        Returns the number of chunks created.
        """
        if metadata is None:
            metadata = {}
        
        # Split into chunks for better search (max 2000 chars per chunk)
        chunks = self._split_text(content, max_chunk_size=2000)
        
        embeddings = [self._generate_embedding(chunk) for chunk in chunks]
        
        self.collections["external_docs"].add(
            ids=[f"{doc_id}_chunk_{i}" for i in range(len(chunks))],
            embeddings=embeddings,
            documents=chunks,
            metadatas=[{
                "type": "external_doc",
                "source": source,
                "timestamp": datetime.now().isoformat(),
                **metadata
            } for _ in chunks]
        )
        
        logger.info("Indexed document %s: %d chunks from %s", doc_id, len(chunks), source)
        return len(chunks)
    
    async def add_personal_note(self, note_id: str, content: str, 
                               tags: List[str] = None) -> None:
        """Add a personal note or reminder."""
        if tags is None:
            tags = []
        
        embedding = self._generate_embedding(content)
        
        self.collections["personal_notes"].add(
            ids=[note_id],
            embeddings=[embedding],
            documents=[content],
            metadatas=[{
                "type": "personal_note",
                "tags": ",".join(tags),
                "timestamp": datetime.now().isoformat()
            }]
        )
        
        logger.info("Added personal note: %s", note_id)
    
    async def lookup(self, query: str, top_k: int = 5,
                    collections: Optional[List[str]] = None) -> List[SearchResult]:
        """Search across collections and return ranked results."""
        if collections is None:
            collections = list(self.collections.keys())
        
        query_embedding = self._generate_embedding(query)
        results = []
        
        for collection_name in collections:
            if collection_name not in self.collections:
                logger.warning("Collection %s not found", collection_name)
                continue
            
            collection = self.collections[collection_name]
            
            try:
                response = collection.query(
                    query_embeddings=[query_embedding],
                    n_results=top_k,
                    include=["documents", "metadatas", "distances"]
                )
                
                if response["documents"] and response["documents"][0]:
                    for i, doc in enumerate(response["documents"][0]):
                        metadata = response["metadatas"][0][i] if response["metadatas"] else {}
                        distance = response["distances"][0][i] if response["distances"] else 0.0
                        
                        # Convert distance to similarity score (lower distance = higher score)
                        score = 1.0 / (1.0 + distance)
                        
                        results.append(SearchResult(
                            collection=collection_name,
                            id=response["ids"][0][i],
                            text=doc,
                            metadata=metadata,
                            score=score
                        ))
            except Exception as e:
                logger.error("Error searching collection %s: %s", collection_name, e)
        
        # Sort by score and return top_k
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]
    
    async def summarize_findings(self, query: str, 
                                results: List[SearchResult],
                                max_results: int = 5) -> str:
        """Ask Bob to summarize the search results for the user."""
        from openai import AsyncOpenAI
        from config import CHAIRMAN_CONFIG
        
        if not results:
            return "Bob found nothing relevant in the archives."
        
        # Format results for Bob
        formatted_results = []
        for i, result in enumerate(results[:max_results], 1):
            source_info = result.metadata.get("source", "Unknown source")
            if result.metadata.get("type") == "speaker_chunk":
                source_info = f"{result.metadata.get('speaker', 'Unknown')} in moot about '{result.metadata.get('topic', 'Unknown')}'"
            
            formatted_results.append(
                f"--- Result {i} (score: {result.score:.2f}) ---\n"
                f"Source: {source_info}\n"
                f"Content: {result.text[:500]}{'...' if len(result.text) > 500 else ''}"
            )
        
        context = "\n\n".join(formatted_results)
        
        client = AsyncOpenAI(
            base_url=CHAIRMAN_CONFIG.base_url,
            api_key=CHAIRMAN_CONFIG.api_key
        )
        
        prompt = (
            f"Search query: {query}\n\n"
            f"Found {len(results)} relevant results from the archives:\n\n"
            f"{context}\n\n"
            f"Summarize these findings for the user. Be concise and highlight the key points. "
            f"Organize by theme or topic if there are multiple relevant discussions. "
            f"Keep it under 300 words."
        )
        
        messages = [
            {"role": "system", "content": CHAIRMAN_CONFIG.system_prompt},
            {"role": "user", "content": prompt}
        ]
        
        try:
            response = await client.chat.completions.create(
                model=CHAIRMAN_CONFIG.model,
                messages=messages,
                max_tokens=400,
                temperature=0.5
            )
            
            summary = response.choices[0].message.content.strip()
            return f"**Searching the archives...**\n\nFound {len(results)} relevant results.\n\n{summary}"
        except Exception as e:
            logger.error("Failed to summarize findings: %s", e)
            # Fallback: return raw results
            raw_text = "\n\n".join([
                f"[{r.metadata.get('speaker', r.collection)}] {r.text[:200]}"
                for r in results[:3]
            ])
            return f"**Searching the archives...**\n\nFound {len(results)} relevant results:\n\n{raw_text}"
    
    def _split_text(self, text: str, max_chunk_size: int = 2000) -> List[str]:
        """Split text into chunks by paragraph, respecting max_chunk_size."""
        paragraphs = text.split('\n\n')
        chunks = []
        current_chunk = []
        current_size = 0
        
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            
            para_size = len(para)
            
            if current_size + para_size > max_chunk_size and current_chunk:
                chunks.append('\n\n'.join(current_chunk))
                current_chunk = [para]
                current_size = para_size
            else:
                current_chunk.append(para)
                current_size += para_size
        
        if current_chunk:
            chunks.append('\n\n'.join(current_chunk))
        
        # If still too large, split by character limit
        final_chunks = []
        for chunk in chunks:
            if len(chunk) > max_chunk_size:
                # Split by sentences
                sentences = chunk.split('. ')
                sub_chunk = []
                sub_size = 0
                for sent in sentences:
                    sent = sent + '.'
                    sent_size = len(sent)
                    if sub_size + sent_size > max_chunk_size and sub_chunk:
                        final_chunks.append(' '.join(sub_chunk))
                        sub_chunk = [sent]
                        sub_size = sent_size
                    else:
                        sub_chunk.append(sent)
                        sub_size += sent_size
                if sub_chunk:
                    final_chunks.append(' '.join(sub_chunk))
            else:
                final_chunks.append(chunk)
        
        return final_chunks if final_chunks else [text]
    
    def get_stats(self) -> Dict:
        """Get statistics about the vector database."""
        stats = {}
        for name, collection in self.collections.items():
            stats[name] = {
                "count": collection.count(),
                "metadata": collection.metadata
            }
        return stats
