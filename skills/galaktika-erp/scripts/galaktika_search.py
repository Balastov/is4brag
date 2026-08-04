#!/usr/bin/env python3
"""
Гибридный поиск по документации Галактика ERP (семантика + BM25).
Использует локальный индекс (эмбеддинги + BM25 + чанки).

Использование:
    python3 galaktika_search.py "текст запроса" [--top-k 10] [--json]
    
При первом запуске загружает модель и индекс (~5-10 сек).
"""

import json
import os
import sys
import pickle
import time

# === Настройки ===
INDEX_DIR = os.environ.get(
    "GALAKTIKA_INDEX_DIR",
    "/mnt/write/KISU Metro/Обучающие материалы по Галактике/Галактика ERP справочная подсистема/index"
)
SEMANTIC_WEIGHT = 0.6   # вес семантического поиска
BM25_WEIGHT = 0.4       # вес BM25


# === Глобальный кэш (загружается один раз) ===
_cache = {}


def load_index():
    """Загружает всё необходимое один раз."""
    try:
        import numpy as np
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "Galaktika standalone search requires numpy and sentence-transformers"
        ) from exc
    if _cache:
        return _cache
    
    print("Загрузка индекса...", file=sys.stderr)
    start = time.time()
    
    # Метаданные
    with open(os.path.join(INDEX_DIR, "index_meta.json")) as f:
        _cache['meta'] = json.load(f)
    
    # Чанки
    with open(os.path.join(INDEX_DIR, "chunks_index.json")) as f:
        _cache['chunks'] = json.load(f)
    
    # Эмбеддинги
    _cache['embeddings'] = np.load(os.path.join(INDEX_DIR, "embeddings.npy"))
    
    # BM25
    with open(os.path.join(INDEX_DIR, "bm25_index.pkl"), 'rb') as f:
        bm25_data = pickle.load(f)
    _cache['bm25'] = bm25_data['bm25']
    _cache['tokenized'] = bm25_data['tokenized']
    
    # Модель
    _cache['model'] = SentenceTransformer(_cache['meta']['model'])
    
    elapsed = time.time() - start
    print(f"Индекс загружен за {elapsed:.0f} сек", file=sys.stderr)
    
    return _cache


def semantic_search(query, cache, top_k=30):
    """Поиск по косинусному сходству эмбеддингов."""
    import numpy as np
    model = cache['model']
    embeddings = cache['embeddings']
    
    q_emb = model.encode([query])[0]
    
    # Косинусное сходство
    q_norm = q_emb / (np.linalg.norm(q_emb) + 1e-8)
    d_norm = embeddings / (np.linalg.norm(embeddings, axis=1, keepdims=True) + 1e-8)
    scores = np.dot(d_norm, q_norm)
    
    # Top-k индексы
    top_indices = np.argsort(-scores)[:top_k]
    
    results = []
    for idx in top_indices:
        results.append({
            'chunk_id': int(idx),
            'score': float(scores[idx]),
            'source': 'semantic'
        })
    
    return results


def bm25_search(query, cache, top_k=30):
    """Поиск через BM25."""
    import numpy as np
    bm25 = cache['bm25']
    tokenized_query = query.lower().split()
    
    scores = bm25.get_scores(tokenized_query)
    
    # Top-k индексы
    top_indices = np.argsort(-scores)[:top_k]
    
    max_score = float(np.max(scores)) if np.max(scores) > 0 else 1.0
    
    results = []
    for idx in top_indices:
        if scores[idx] > 0:
            results.append({
                'chunk_id': int(idx),
                'score': float(scores[idx]) / max_score,
                'source': 'bm25'
            })
    
    return results


def hybrid_fusion(semantic_results, bm25_results, top_k=10):
    """
    Reciprocal Rank Fusion: объединяет результаты семантического и BM25 поиска.
    """
    scores = {}  # chunk_id → финальный счёт
    
    for rank, r in enumerate(semantic_results):
        cid = r['chunk_id']
        scores[cid] = scores.get(cid, 0) + SEMANTIC_WEIGHT * (1.0 / (rank + 1))
    
    for rank, r in enumerate(bm25_results):
        cid = r['chunk_id']
        scores[cid] = scores.get(cid, 0) + BM25_WEIGHT * (1.0 / (rank + 1))
    
    sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
    
    results = []
    for cid in sorted_ids[:top_k]:
        results.append({
            'chunk_id': cid,
            'fusion_score': round(scores[cid], 4)
        })
    
    return results


def search(query, top_k=10, verbose=False):
    """Гибридный поиск: семантика + BM25."""
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
    
    # Обогащаем результатами из чанков
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
    """Форматирует результаты для подачи в LLM."""
    if not results:
        return f"По запросу «{query}» ничего не найдено в документации Галактика ERP."
    
    buf = []
    buf.append("# Результаты поиска по документации Галактика ERP\n")
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
        buf.append(f"*Источник:* http://31.128.50.73/assets/help/{r['url']}")
        buf.append(f"*Релевантность:* {r['score']}")
        buf.append("")
        buf.append(text)
        buf.append("")
    
    return "\n".join(buf)


def main():
    if len(sys.argv) < 2:
        print("Usage: galaktika_search.py QUERY [--top-k N] [--json] [--verbose]", file=sys.stderr)
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
            'url': f"http://31.128.50.73/assets/help/{r['url']}",
            'text': r['text'][:2000],
            'score': r['score']
        } for r in results]
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(format_context(results, query))


if __name__ == "__main__":
    main()
