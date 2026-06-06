"""Model wrappers. Each is lazily loaded so importing the package is cheap and the heavy
dependency (fastembed, etc.) is only required when the model is actually used. Phase 1 ships the
text embedder and the Ollama chat client; VLM/CLIP/OCR/reranker + the GPU lease arrive next."""
