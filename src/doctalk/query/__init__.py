"""Query layer: retrieval over the LanceDB index joined back to MySQL, and RAG chat that answers
with citations to real (file, chapter, page) locations. ``prompt`` holds the pure prompt-assembly
helpers; ``retriever`` does ANN + join; ``chat`` ties retrieve -> answer together."""
