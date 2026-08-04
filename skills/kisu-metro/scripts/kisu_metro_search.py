#!/usr/bin/env python3
"""
Гибридный поиск по 5 разделам KISU Metro (семантика + TF-IDF).
Объединяет: Стадии проекта, Спецификации требований, Архитектура,
           Управление проектом, Термины и сокращения.

Использование:
    python3 kisu_metro_search.py "текст запроса" [--top-k 10] [--json] [--section "Стадии проекта"]

При первом запуске загружает индексы и модель (~35 сек).
Общий объём: 18 964 чанка (6 331 Стадии проекта + 2 153 Спецификации + 3 375 Архитектура + 7 054 Управление проектом + 51 Термины), эмбеддинги 1024d (multilingual-e5-large).

"""

import json
import os
import sys
import pickle
import time

# === Настройки ===
BASE_DIR = os.getenv("KISU_METRO_BASE", "/mnt/write/KISU Metro")
ALL_SECTIONS = [
    "Стадии проекта",
    "KISU Metro - Спецификации требований",
    "Архитектура",
    "Управление проектом",
    "Термины и сокращения",
]
SEMANTIC_WEIGHT = 0.6
TFIDF_WEIGHT = 0.4
E5_QUERY_PREFIX = "query: "

# === Глобальный кэш ===
_cache = {}          # section → {chunks, embeddings, vectorizer, matrix}
_parents = {}        # section → {page_id: parent}
_model = None        # общая модель (одна на все разделы)
_np = None


def load_section(section: str) -> dict:
    """Загружает индекс одного раздела."""
    global _np
    if _np is None:
        import numpy
        _np = numpy
    idx_dir = os.path.join(BASE_DIR, section)

    meta_path = os.path.join(idx_dir, "index_meta.json")
    if not os.path.exists(meta_path):
        print(f"⚠️  Индекс не найден: {section}", file=sys.stderr)
        return None

    with open(meta_path) as f:
        meta = json.load(f)

    with open(os.path.join(idx_dir, "chunks_index.json")) as f:
        chunks = json.load(f)

    embeddings = _np.load(os.path.join(idx_dir, "embeddings.npy"))

    with open(os.path.join(idx_dir, "bm25_index.pkl"), "rb") as f:
        bm25_data = pickle.load(f)

    if not meta.get("e5_prefixes"):
        print(f"⚠️  {section}: индекс без e5_prefixes — качество семантики "
              f"снижено до полной переиндексации", file=sys.stderr)

    return {
        "section": section,
        "meta": meta,
        "chunks": chunks,
        "embeddings": embeddings,
        "vectorizer": bm25_data["vectorizer"],
        "matrix": bm25_data["matrix"],
        "n_chunks": len(chunks),
        "dim": embeddings.shape[1],
    }


def load_all_indexes(sections=None):
    """Загружает индексы и модель. Кэширует в _cache."""
    global _model, _cache

    if sections is None:
        sections = ALL_SECTIONS

    new_sections = [s for s in sections if s not in _cache]
    if not new_sections and _model is not None:
        return  # всё уже загружено

    start = time.time()
    print("Загрузка индексов KISU Metro...", file=sys.stderr)

    # Загружаем модель один раз
    if _model is None:
        from sentence_transformers import SentenceTransformer
        for s in ALL_SECTIONS:
            meta_path = os.path.join(BASE_DIR, s, "index_meta.json")
            if os.path.exists(meta_path):
                with open(meta_path) as f:
                    meta = json.load(f)
                model_name = meta.get("embedding_model", "intfloat/multilingual-e5-large")
                print(f"  Загрузка модели: {model_name}...", file=sys.stderr)
                _model = SentenceTransformer(model_name)
                break

    total = 0
    for section in new_sections:
        data = load_section(section)
        if data:
            _cache[section] = data
            total += data["n_chunks"]
            print(f"  {section}: {data['n_chunks']:,} чанков ({data['dim']}d)", file=sys.stderr)

    elapsed = time.time() - start
    print(f"  Загружено за {elapsed:.0f} сек | Всего: {total:,} чанков\n", file=sys.stderr)


def load_parents(section: str) -> dict:
    """Загружает parents.json для раздела (кэширует)."""
    global _parents
    if section in _parents:
        return _parents[section]
    path = os.path.join(BASE_DIR, section, "parents.json")
    if not os.path.exists(path):
        print(f"⚠️  parents.json не найден: {section}", file=sys.stderr)
        return {}
    with open(path) as f:
        _parents[section] = json.load(f)
    return _parents[section]


def resolve_parents(results: list) -> list:
    """
    Parent-child retrieval: заменяет чанки на полные страницы.
    Дедупликация по page_id, сохранение лучшего скора.
    """
    seen = {}
    unique = []
    for r in results:
        pid = r.get("page_id", "")
        if not pid:
            unique.append(r)
            continue
        if pid in seen:
            # Обновляем скор, если этот чанк релевантнее
            if r["score"] > seen[pid]["score"]:
                seen[pid]["score"] = r["score"]
            continue
        seen[pid] = r

    # Загружаем родителей и заменяем текст чанка на полный текст страницы
    for pid, best in seen.items():
        parents = load_parents(best["section"])
        parent = parents.get(pid)
        if parent:
            unique.append({
                "page_id": pid,
                "section": best["section"],
                "title": parent["title"],
                "url": parent["url"],
                "breadcrumbs": parent.get("breadcrumbs") or best.get("breadcrumbs", ""),
                "text": parent["full_text"],
                "score": best["score"],
                "source": "parent",  # помечаем, что это полная страница
            })
        else:
            unique.append(best)  # fallback: оставляем чанк

    # Сортируем по скору
    unique.sort(key=lambda x: x["score"], reverse=True)
    return unique


def semantic_search(query: str, section_data: dict, top_k: int = 30) -> list:
    """Косинусное сходство эмбеддингов по одному разделу."""
    global _model
    embeddings = section_data["embeddings"]

    # multilingual-e5: документы с «passage:», запросы с «query:»
    # Для старых индексов без e5_prefixes — кодируем запрос как раньше (без префикса)
    q_text = (E5_QUERY_PREFIX + query) if section_data.get("meta", {}).get("e5_prefixes") else query
    q_emb = _model.encode([q_text])[0]
    q_norm = q_emb / (_np.linalg.norm(q_emb) + 1e-8)
    d_norm = embeddings / (_np.linalg.norm(embeddings, axis=1, keepdims=True) + 1e-8)
    scores = _np.dot(d_norm, q_norm)

    top_indices = _np.argsort(-scores)[:top_k]
    return [
        {"chunk_id": int(idx), "score": float(scores[idx]), "source": "semantic",
         "section": section_data["section"]}
        for idx in top_indices
    ]


def tfidf_search(query: str, section_data: dict, top_k: int = 30) -> list:
    """Косинусное сходство через TF-IDF векторы."""
    vectorizer = section_data["vectorizer"]
    matrix = section_data["matrix"]

    q_vec = vectorizer.transform([query])
    scores = (matrix @ q_vec.T).toarray().ravel()

    top_indices = _np.argsort(-scores)[:top_k]
    max_score = float(_np.max(scores)) if _np.max(scores) > 0 else 1.0

    return [
        {"chunk_id": int(idx), "score": float(scores[idx]) / max_score, "source": "tfidf",
         "section": section_data["section"]}
        for idx in top_indices if scores[idx] > 0
    ]


def hybrid_fusion(sem_results: list, tfidf_results: list, top_k: int = 10) -> list:
    """
    Score-augmented Reciprocal Rank Fusion.
    
    Формула: weight × raw_score / (K + section_rank + 1)
    
    Где:
    - raw_score: для семантики — raw-косинус (0..1), для TF-IDF — нормализованный score
    - section_rank: per-section взвешенный ранг. #1 в разделе = 0, далее убывает с шагом 1/w.
    - K = 60 (классическая RRF-константа)
    
    Множитель raw_score защищает от irrelevant-мусора:
    документ с косинусом 0.55 на #1 месте проигрывает документу с 0.85 на #2.
    """
    scores = {}
    RRF_K = 60

    for r in sem_results:
        key = (r["section"], r["chunk_id"])
        rank = r.get("section_rank", 0)
        raw_score = r.get("score", 0)
        contrib = SEMANTIC_WEIGHT * raw_score / (RRF_K + rank + 1)
        scores[key] = max(scores.get(key, 0), contrib)

    for r in tfidf_results:
        key = (r["section"], r["chunk_id"])
        rank = r.get("section_rank", 0)
        raw_score = r.get("score", 0)
        contrib = TFIDF_WEIGHT * raw_score / (RRF_K + rank + 1)
        scores[key] = max(scores.get(key, 0), contrib)

    sorted_keys = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)

    results = []
    for (section, chunk_id) in sorted_keys[:top_k]:
        results.append({
            "section": section,
            "chunk_id": chunk_id,
            "fusion_score": round(scores[(section, chunk_id)], 4),
        })
    return results


def legacy_search(query: str, top_k: int = 10, sections: list = None,
                  verbose: bool = False, use_parents: bool = True):
    """Гибридный поиск по всем (или указанным) разделам.
    
    use_parents=True: возвращает полные страницы (parent-child retrieval).
    use_parents=False: возвращает отдельные чанки (как раньше).
    """
    global _cache

    if sections is None:
        sections = ALL_SECTIONS

    load_all_indexes(sections)

    all_sem = []
    all_tfidf = []

    for section in sections:
        data = _cache.get(section)
        if data is None:
            continue

        sem = semantic_search(query, data, top_k=30)
        tfidf = tfidf_search(query, data, top_k=30)
        all_sem.extend(sem)
        all_tfidf.extend(tfidf)

        if verbose:
            print(f"[{section}] семантика: {len(sem)}, TF-IDF: {len(tfidf)}", file=sys.stderr)

    # === Per-section нормализация рангов со взвешиванием ===
    # Каждый раздел получает per-section ранги, но с весом пропорциональным
    # log(размер_секции). Это даёт крупным разделам небольшое преимущество
    # (у них больше шансов найти релевантный контент), но не позволяет
    # доминировать полностью как раньше.
    import math as _math
    
    # Считаем размер каждой секции
    sec_sizes = {}
    for r in all_sem:
        sec_sizes[r["section"]] = len(_cache[r["section"]]["chunks"])
    for r in all_tfidf:
        sec_sizes[r["section"]] = len(_cache[r["section"]]["chunks"])
    
    # Вес секции = log(размер) / log(макс_размер), диапазон [0, 1]
    max_log = _math.log(max(sec_sizes.values())) if sec_sizes else 1.0
    
    def _section_weight(sec):
        return _math.log(sec_sizes.get(sec, 1)) / max_log if max_log > 0 else 1.0
    
    # Группируем и ранжируем внутри секций
    sem_by_sec = {}
    tfidf_by_sec = {}
    for r in all_sem:
        sem_by_sec.setdefault(r["section"], []).append(r)
    for r in all_tfidf:
        tfidf_by_sec.setdefault(r["section"], []).append(r)
    
    all_sem = []
    for sec, sec_results in sem_by_sec.items():
        w = _section_weight(sec)
        sec_results.sort(key=lambda x: x["score"], reverse=True)
        for rank, r in enumerate(sec_results):
            r["section_rank"] = rank / max(w, 0.01)
            all_sem.append(r)
    
    all_tfidf = []
    for sec, sec_results in tfidf_by_sec.items():
        w = _section_weight(sec)
        sec_results.sort(key=lambda x: x["score"], reverse=True)
        for rank, r in enumerate(sec_results):
            r["section_rank"] = rank / max(w, 0.01)
            all_tfidf.append(r)
    # === Конец нормализации ===

    fused = hybrid_fusion(all_sem, all_tfidf, top_k)

    if verbose:
        print(f"Итого RRF: {len(fused)} результатов", file=sys.stderr)

    # Обогащаем текстом из чанков
    results = []
    for r in fused:
        data = _cache[r["section"]]
        chunk = data["chunks"][r["chunk_id"]]
        results.append({
            "chunk_id": chunk["chunk_id"],
            "section": r["section"],
            "title": chunk.get("title", ""),
            "url": chunk.get("url", ""),
            "page_id": chunk.get("page_id", ""),
            "breadcrumbs": chunk.get("breadcrumbs", ""),
            "text": chunk["text"],
            "score": r["fusion_score"],
        })

    # Parent-child: заменяем чанки на полные страницы
    if use_parents:
        results = resolve_parents(results)

    return results


def _api_search(query: str, top_k: int, sections: list, use_parents: bool) -> list:
    """Use only HTTP here; never invoke this script through a subprocess."""
    from urllib.request import Request, urlopen

    url = os.environ["SEARCH_API_URL"].rstrip("/") + "/search"
    timeout = float(os.getenv("SEARCH_API_TIMEOUT", "15"))
    payload = json.dumps({
        "query": query,
        "top_k": top_k,
        "sections": sections,
        "use_parents": use_parents,
    }).encode("utf-8")
    request = Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=timeout) as response:
        decoded = json.loads(response.read().decode("utf-8"))
    if not isinstance(decoded, dict) or not isinstance(decoded.get("results"), list):
        raise RuntimeError("search API returned an invalid response")
    return decoded["results"]


def search(query: str, top_k: int = 10, sections: list = None,
           verbose: bool = False, use_parents: bool = True):
    api_url = os.getenv("SEARCH_API_URL", "").strip()
    if api_url:
        try:
            return _api_search(query, top_k, sections, use_parents)
        except Exception as exc:
            fallback = os.getenv("SEARCH_API_LEGACY_FALLBACK", "0").lower()
            if fallback not in {"1", "true", "yes", "on"}:
                raise
            if verbose:
                print("Search API unavailable, using legacy indexes: %s" % exc, file=sys.stderr)
    return legacy_search(query, top_k, sections, verbose, use_parents)


def format_context(results: list, query: str, use_parents: bool = False) -> str:
    source_label = "страниц" if use_parents else "чанков"
    if not results:
        return f"По запросу «{query}» ничего не найдено в разделах KISU Metro."

    buf = []
    buf.append("# Результаты поиска по KISU Metro\n")
    buf.append(f"**Запрос:** «{query}»\n")
    buf.append(f"**Найдено:** {len(results)} {source_label}\n")

    for i, r in enumerate(results, 1):
        text = r["text"]
        max_len = 8000 if use_parents else 3000
        if len(text) > max_len:
            text = text[:max_len] + "... [текст обрезан]"

        buf.append("---")
        buf.append(f"## {i}. {r['title']}")
        buf.append(f"*Раздел:* {r['section']}  |  *Релевантность:* {r['score']}")
        if r.get("breadcrumbs"):
            buf.append(f"*Путь:* {r['breadcrumbs']}")
        if r["url"]:
            buf.append(f"*Источник:* {r['url']}")
        if r.get("page_id"):
            buf.append(f"*Page ID:* {r['page_id']}")
        if r.get("source"):
            buf.append(f"*Тип:* {r['source']}")
        buf.append("")
        buf.append(text)
        buf.append("")

    return "\n".join(buf)


def main():
    if len(sys.argv) < 2:
        print("Usage: kisu_metro_search.py QUERY [--top-k N] [--json] [--verbose] [--section NAME] [--parent/--no-parents]",
              file=sys.stderr)
        sys.exit(1)

    query = sys.argv[1]
    top_k = 10
    output_json = False
    verbose = False
    sections = None
    use_parents = True  # по умолчанию — parent-child

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
        elif args[i] == "--parent":
            use_parents = True
            i += 1
        elif args[i] == "--no-parents":
            use_parents = False
            i += 1
        elif args[i] == "--section" and i + 1 < len(args):
            sections = [args[i + 1]]
            i += 2
        elif args[i] == "--all":
            sections = ALL_SECTIONS
            i += 1
        else:
            i += 1

    results = search(query, top_k=top_k, sections=sections, verbose=verbose,
                     use_parents=use_parents)

    if output_json:
        out = []
        for r in results:
            item = {
                "section": r["section"],
                "title": r["title"],
                "url": r["url"],
                "page_id": r.get("page_id", ""),
                "breadcrumbs": r.get("breadcrumbs", ""),
                "text": r["text"][:2000],
                "score": r["score"],
            }
            if "chunk_id" in r:
                item["chunk_id"] = r["chunk_id"]
            if "source" in r:
                item["source"] = r["source"]
            out.append(item)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(format_context(results, query, use_parents))


if __name__ == "__main__":
    main()
