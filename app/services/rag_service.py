from app.services.ai_client import AIClient
from app import supabase
from docx import Document
import os

class RAGService:
    @staticmethod
    def get_embedding(text, task_type="retrieval_document"):
        """Generates embeddings using Gemini's text-embedding-004 model (768 dimensions)."""
        return AIClient.get_embedding(text, task_type)

    @staticmethod
    def ingest_docx(file_path, department_id, filename):
        """Reads a DOCX file, chunks it, embeds it, and stores it in Supabase."""
        try:
            doc = Document(file_path)
            full_text = []
            for para in doc.paragraphs:
                if para.text.strip():
                    full_text.append(para.text)
            
            # Semantic chunking (simple approximation: combine paragraphs until ~1000 chars)
            chunks = []
            current_chunk = ""
            for line in full_text:
                if len(current_chunk) + len(line) < 1000:
                    current_chunk += line + "\n"
                else:
                    chunks.append(current_chunk)
                    current_chunk = line + "\n"
            if current_chunk:
                chunks.append(current_chunk)

            records = []
            for chunk in chunks:
                embedding = RAGService.get_embedding(chunk, task_type="retrieval_document")
                if embedding:
                    records.append({
                        "department_id": department_id,
                        "filename": filename,
                        "content": chunk,
                        "embedding": embedding
                    })
            
            if records:
                response = supabase.table('knowledge_base').insert(records).execute()
                return len(records)
            return 0
        except Exception as e:
            print(f"Ingestion Error: {e}")
            return 0

    @staticmethod
    def query_knowledge_base(query, department_id, limit=3):
        """Retrieves relevant context from the knowledge base."""
        try:
            # Embed the query
            query_embedding = RAGService.get_embedding(query, task_type="retrieval_query")
            
            if not query_embedding:
                return []

            # Call the Supabase RPC function 'match_documents'
            response = supabase.rpc(
                'match_documents',
                {
                    'query_embedding': query_embedding,
                    'match_threshold': 0.5, # Adjust threshold based on testing
                    'match_count': limit,
                    'filter_department_id': department_id
                }
            ).execute()
            
            return response.data
        except Exception as e:
            print(f"RAG Query Error: {e}")
            return []
