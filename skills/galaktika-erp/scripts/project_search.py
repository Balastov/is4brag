#!/usr/bin/env python3
"""
Гибридный поиск по Проектным решениям Stage 6 (семантика + BM25).
Аналог galaktika_search.py, адаптированный для ПР.

Использование:
    python3 project_search.py "текст запроса" [--top-k 10] [--json]
    
При первом запуске загружает модель и индекс (~5-10 сек).
В индексе: 9 953 чанка из 1 425 страниц Confluence.
"""

import json
import os
import sys
import pickle
import time
import numpy as np
from sentence_transformers import SentenceTransformer

# === Настройки ===
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_DIR = os.environ.get(
    "PR_INDEX_DIR",
    os.path.join(os.path.dirname(SCRIPT_DIR), "index")
)
SEMANTIC_WEIGHT = 0.6
BM25_WEIGHT = 0.4

# === Глобальный кэш ===
_cache = {}


def load_index():
    if _cache:
        return _cache
    
    print("Загрузка индекса ПР Stage 6...", file=sys.stderr)
    start = time.time()
    
    with open(os.path.join(INDEX_DIR, "index_meta.json")) as f:
        _cache['meta'] = json.load(f)
    
    with open(os.path.join(INDEX_DIR, "chunks_index.json")) as f:
        _cache['chunks'] = json.load(f)
    
    _cache['embeddings'] = np.load(os.path.join(INDEX_DIR, "embeddings.npy"))
    
    with open(os.path.join(INDEX_DIR, "bm25_index.pkl"), 'rb') as f:
        bm25_data = pickle.load(f)
    _cache['bm25'] = bm25_data['bm25']
    _cache['tokenized'] = bm25_data['tokenized']
    
    _cache['model'] = SentenceTransformer(_cache['meta']['model'])
    
    elapsed = time.time() - start
    print(f"Индекс загружен за {elapsed:.0f} сек", file=sys.stderr)
    return _cache


def semantic_search(query, cache, top_k=30):
    model = cache['model']
    embeddings = cache['embeddings']
    q_emb = model.encode([query])[0]
    
    q_norm = q_emb / (np.linalg.norm(q_emb) + 1e-8)
    d_norm = embeddings / (np.linalg.norm(embeddings, axis=1, keepdims=True) + 1e-8)
    scores = np.dot(d_norm, q_norm)
    
    top_indices = np.argsort(-scores)[:top_k]
    return [{'chunk_id': int(idx), 'score': float(scores[idx]), 'source': 'semantic'} 
            for idx in top_indices]


def bm25_search(query, cache, top_k=30):
    bm25 = cache['bm25']
    tokenized_query = query.lower().split()
    scores = bm25.get_scores(tokenized_query)
    top_indices = np.argsort(-scores)[:top_k]
    max_score = float(np.max(scores)) if np.max(scores) > 0 else 1.0
    
    return [{'chunk_id': int(idx), 'score': float(scores[idx]) / max_score, 'source': 'bm25'}
            for idx in top_indices if scores[idx] > 0]


def hybrid_fusion(semantic_results, bm25_results, top_k=10):
    scores = {}
    for rank, r in enumerate(semantic_results):
        scores[r['chunk_id']] = scores.get(r['chunk_id'], 0) + SEMANTIC_WEIGHT * (1.0 / (rank + 1))
    for rank, r in enumerate(bm25_results):
        scores[r['chunk_id']] = scores.get(r['chunk_id'], 0) + BM25_WEIGHT * (1.0 / (rank + 1))
    
    sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
    return [{'chunk_id': cid, 'fusion_score': round(scores[cid], 4)} for cid in sorted_ids[:top_k]]


def search(query, top_k=10, verbose=False):
    cache = load_index()
    
    if verbose:
        print(f"Поиск: «{query}»", file=sys.stderr)
    
    semantic_results = semantic_search(query, cache, top_k=30)
    bm25_results = bm25_search(query, cache, top_k=30)
    
    if verbose:
        print(f"Семантика: {len(semantic_results)} результатов", file=sys.stderr)
        print(f"BM25:     {len(bm25_results)} результатов", file=sys.stderr)
    
    fused = hybrid_fusion(semantic_results, bm25_results, top_k)
    
    if verbose:
        print(f"Итого:    {len(fused)} результатов", file=sys.stderr)
    
    chunks = cache['chunks']
    results = []
    for r in fused:
        chunk = chunks[r['chunk_id']]
        results.append({
            'chunk_id': chunk['chunk_id'],
            'title': chunk['title'],
            'breadcrumbs': chunk['breadcrumbs'],
            'url': chunk['url'],
            'text': chunk['text'],
            'score': r['fusion_score'],
            'char_len': chunk['char_len']
        })
    return results


def format_context(results, query):
    if not results:
        return f"По запросу «{query}» ничего не найдено в Проектных решениях Stage 6."
    
    buf = []
    buf.append("# Результаты поиска по Проектным решениям Stage 6\n")
    buf.append(f"**Запрос:** «{query}»\n")
    buf.append(f"**Найдено:** {len(results)} чанков\n")
    
    for i, r in enumerate(results, 1):
        text = r['text']
        if len(text) > 3000:
            text = text[:3000] + "... [текст обрезан]"
        
        buf.append("---")
        buf.append(f"## {i}. {r['title']}")
        if r['breadcrumbs']:
            buf.append(f"*Раздел:* {r['breadcrumbs']}")
        buf.append(f"*Источник:* {r['url']}")
        buf.append(f"*Релевантность:* {r['score']}")
        buf.append("")
        buf.append(text)
        buf.append("")
    
    return "\n".join(buf)


def main():
    if len(sys.argv) < 2:
        print("Usage: project_search.py QUERY [--top-k N] [--json] [--verbose]", file=sys.stderr)
        sys.exit(1)
    
    query = sys.argv[1]
    top_k = 10
    output_json = False
    verbose = False
    
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--top-k" and i + 1 < len(args):
            top_k = int(args[i + 1])
            i += 2
        elif args[i] == "--json":
            output_json = True
            i += 1
        elif args[i] == "--verbose":
            verbose = True
            i += 1
        else:
            i += 1
    
    results = search(query, top_k, verbose)
    
    if output_json:
        out = [{
            'chunk_id': r['chunk_id'],
            'title': r['title'],
            'breadcrumbs': r['breadcrumbs'],
            'url': r['url'],
            'text': r['text'][:2000],
            'score': r['score']
        } for r in results]
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(format_context(results, query))


if __name__ == "__main__":
    main()
