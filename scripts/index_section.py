#!/usr/bin/env python3
"""Автономный построитель индексов для KISU Metro.
   Запускать на хосте: python3 index_section.py <имя_раздела>
   Пример: python3 index_section.py "Стадии проекта"
"""
import json, os, sys, pickle, time, argparse

# --- Ограничение потоков BLAS/OpenMP (добавлено 2026-07-20) ---
# torch/numpy по умолчанию поднимают потоков по числу ядер (здесь 8). При параллельных
# запусках процессы дерутся за CPU, и переключения контекста съедают больше, чем даёт
# параллелизм. Должно стоять ДО импорта numpy/sentence_transformers.
for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "4")
# --- конец правки ---

import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.feature_extraction.text import TfidfVectorizer

BASE = os.path.dirname(os.path.abspath(__file__))  # каталог KISU Metro
MODEL_NAME = "intfloat/multilingual-e5-large"
BATCH_SIZE = 32

def build(section: str, device: str = "cpu", page_ids: set = None):
    """Полная или инкрементальная индексация.
    
    Если page_ids передан — инкрементальный режим:
    удаляет старые чанки для указанных page_id и добавляет новые.
    Иначе — полная переиндексация (старое поведение).
    """
    chunks_path = os.path.join(BASE, section, "chunks_export.jsonl")
    out_dir = os.path.join(BASE, section)

    if not os.path.exists(chunks_path):
        print(f"❌ Файл не найден: {chunks_path}")
        sys.exit(1)

    # Загрузка новых чанков (полный актуальный список)
    new_chunks_raw = []
    with open(chunks_path, encoding="utf-8") as f:
        for line in f:
            new_chunks_raw.append(json.loads(line))

    # Проверяем, есть ли существующий индекс
    emb_path = os.path.join(out_dir, "embeddings.npy")
    chunks_idx_path = os.path.join(out_dir, "chunks_index.json")
    has_existing = os.path.exists(emb_path) and os.path.exists(chunks_idx_path)

    # ── Инкрементальный режим ──
    if page_ids and has_existing:
        print(f"[{time.strftime('%H:%M:%S')}] Инкрементальная индексация: {section}")
        print(f"  Изменённых page_id: {len(page_ids)}")
        _build_incremental(section, out_dir, chunks_path, new_chunks_raw,
                           page_ids, device)
        return

    # ── Полная переиндексация (старое поведение) ──
    print(f"[{time.strftime('%H:%M:%S')}] Полная индексация: {section}")
    texts = [c["text"] for c in new_chunks_raw]
    N = len(new_chunks_raw)
    total_chars = sum(len(t) for t in texts)
    print(f"  Страниц: {len(set(c.get('page_id','') for c in new_chunks_raw))} | "
          f"Чанков: {N} | Символов: {total_chars:,}")

    _full_index(out_dir, new_chunks_raw, texts, N, section, device)


def _build_incremental(section: str, out_dir: str, chunks_path: str,
                       new_chunks_raw: list, page_ids: set, device: str = "cpu"):
    """Инкрементальное обновление: удалить старые чанки page_ids, добавить новые."""
    import numpy as np
    from sentence_transformers import SentenceTransformer
    from sklearn.feature_extraction.text import TfidfVectorizer

    # 1. Загружаем текущий индекс
    with open(os.path.join(out_dir, "chunks_index.json"), encoding="utf-8") as f:
        old_chunks = json.load(f)

    # 2. Разделяем: чанки на сохранение vs чанки на замену
    keep_indices = []
    new_chunks_for_pages = []  # новые чанки для изменённых страниц
    for i, oc in enumerate(old_chunks):
        if oc.get("page_id", "") in page_ids:
            continue  # удаляем — эти страницы изменились
        keep_indices.append(i)

    for c in new_chunks_raw:
        if c["page_id"] in page_ids:
            new_chunks_for_pages.append(c)

    old_embeddings = np.load(os.path.join(out_dir, "embeddings.npy"))
    kept_embeddings = (old_embeddings[keep_indices]
                       if keep_indices
                       else np.empty((0, old_embeddings.shape[1]), dtype=np.float32))
    kept_chunks = [old_chunks[i] for i in keep_indices]

    stats = f"  Сохранено: {len(keep_indices)} чанков | Новых: {len(new_chunks_for_pages)} чанков"
    if len(keep_indices) + len(old_chunks) > 0:
        removed = len(old_chunks) - len(keep_indices)
        stats += f" | Удалено (старых версий): {removed}"
    print(stats)

    # 3. Кодируем новые чанки
    if new_chunks_for_pages:
        print(f"[{time.strftime('%H:%M:%S')}] Кодирование {len(new_chunks_for_pages)} новых чанков...")
        model = SentenceTransformer(MODEL_NAME, device=device)
        t0 = time.time()
        new_texts = [c["text"] for c in new_chunks_for_pages]
        new_embeddings = model.encode(new_texts, show_progress_bar=True,
                                      batch_size=BATCH_SIZE, normalize_embeddings=True)
        elapsed = time.time() - t0
        print(f"  Готово за {elapsed:.0f}с ({len(new_chunks_for_pages)/elapsed:.0f} чанков/с) "
              f"| shape={new_embeddings.shape}")
    else:
        new_embeddings = np.empty((0, kept_embeddings.shape[1]), dtype=np.float32)

    # 4. Склеиваем
    if len(keep_indices) > 0:
        all_embeddings = np.vstack([kept_embeddings, new_embeddings])
    else:
        all_embeddings = new_embeddings
    all_chunks = kept_chunks + new_chunks_for_pages

    # 5. Перенумерация chunk_id
    for i, c in enumerate(all_chunks):
        c["chunk_id"] = i

    # 6. Перестраиваем BM25 (быстро: O(секунд))
    print(f"[{time.strftime('%H:%M:%S')}] Перестроение BM25...")
    t0 = time.time()
    all_texts = [c["text"] for c in all_chunks]
    vectorizer = TfidfVectorizer(max_features=10000, lowercase=True)
    bm25_matrix = vectorizer.fit_transform(all_texts)
    print(f"  Готово за {time.time()-t0:.1f}с | shape={bm25_matrix.shape}")

    # 7. Сохранение
    _save_index(out_dir, all_embeddings, vectorizer, bm25_matrix,
                all_chunks, section, all_embeddings.shape[1])


def _full_index(out_dir: str, chunks: list, texts: list, N: int,
                section: str, device: str = "cpu"):
    """Полная переиндексация (старое поведение)."""
    import numpy as np
    from sentence_transformers import SentenceTransformer
    from sklearn.feature_extraction.text import TfidfVectorizer

    # Модель
    print(f"[{time.strftime('%H:%M:%S')}] Загрузка модели ({device})...")
    model = SentenceTransformer(MODEL_NAME, device=device)

    # Эмбеддинги
    print(f"[{time.strftime('%H:%M:%S')}] Эмбеддинги ({N} текстов)...")
    t0 = time.time()
    embeddings = model.encode(texts, show_progress_bar=True,
                              batch_size=BATCH_SIZE, normalize_embeddings=True)
    elapsed = time.time() - t0
    print(f"  Готово за {elapsed:.0f}с ({N/elapsed:.0f} чанков/с) | shape={embeddings.shape}")

    # BM25
    print(f"[{time.strftime('%H:%M:%S')}] BM25 индекс...")
    t0 = time.time()
    vectorizer = TfidfVectorizer(max_features=10000, lowercase=True)
    bm25_matrix = vectorizer.fit_transform(texts)
    print(f"  Готово за {time.time()-t0:.1f}с | shape={bm25_matrix.shape}")

    # Индексные чанки
    index_chunks = [
        {"chunk_id": i, "text": c["text"], "title": c["title"],
         "url": c.get("url", ""), "page_id": c.get("page_id", "")}
        for i, c in enumerate(chunks)
    ]

    _save_index(out_dir, embeddings, vectorizer, bm25_matrix,
                index_chunks, section, int(embeddings.shape[1]))


def _save_index(out_dir: str, embeddings, vectorizer, bm25_matrix,
                index_chunks: list, section: str, dim: int):
    """Сохраняет все индексные файлы, включая parents.json для parent-child retrieval."""
    print(f"[{time.strftime('%H:%M:%S')}] Сохранение...")
    import numpy as np
    np.save(os.path.join(out_dir, "embeddings.npy"), embeddings)
    with open(os.path.join(out_dir, "bm25_index.pkl"), "wb") as f:
        pickle.dump({"vectorizer": vectorizer, "matrix": bm25_matrix}, f)
    with open(os.path.join(out_dir, "chunks_index.json"), "w", encoding="utf-8") as f:
        json.dump(index_chunks, f, ensure_ascii=False, indent=2)

    meta = {
        "section": section, "total_chunks": len(index_chunks),
        "embedding_model": MODEL_NAME, "embedding_dim": dim,
        "chunk_size": 800, "chunk_overlap": 150,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "confluence_url": "https://conf-metro.ibs.ru"
    }
    with open(os.path.join(out_dir, "index_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # Размеры
    for fn in ["embeddings.npy", "bm25_index.pkl", "chunks_index.json"]:
        sz = os.path.getsize(os.path.join(out_dir, fn))
        print(f"  {fn}: {sz/1024/1024:.1f} MB")

    # ── parents.json (parent-child retrieval) ──
    t0 = time.time()
    parents = {}
    for c in index_chunks:
        pid = c.get("page_id", "unknown")
        if pid not in parents:
            parents[pid] = {
                "title": c.get("title", ""),
                "url": c.get("url", ""),
                "page_id": pid,
                "chunk_ids": [],
                "full_text": ""
            }
        parents[pid]["chunk_ids"].append(c["chunk_id"])

    # Склеиваем текст чанков → полный текст страницы
    for pid, parent in parents.items():
        texts = [index_chunks[i]["text"] for i in parent["chunk_ids"]]
        parent["full_text"] = "\n\n".join(texts)

    parents_path = os.path.join(out_dir, "parents.json")
    with open(parents_path, "w", encoding="utf-8") as f:
        json.dump(parents, f, ensure_ascii=False, indent=2)

    sz = os.path.getsize(parents_path) / 1024 / 1024
    elapsed = time.time() - t0
    print(f"  parents.json: {sz:.1f} MB | {len(parents):,} страниц за {elapsed:.1f}с")

    print(f"[{time.strftime('%H:%M:%S')}] ✅ {section} — ГОТОВО\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Построение индекса для раздела KISU Metro")
    parser.add_argument("section", help="Имя раздела (папки)")
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda"],
                        help="Устройство для модели (cpu/cuda)")
    parser.add_argument("--pages", default="", type=str,
                        help="Список page_id через запятую для инкрементальной "
                             "индексации (иначе — полная)")
    args = parser.parse_args()

    page_ids = set(args.pages.split(",")) if args.pages else None
    build(args.section, device=args.device, page_ids=page_ids)
