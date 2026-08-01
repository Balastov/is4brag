#!/usr/bin/env python3
"""Resumable index builder для KISU Metro.
Сохраняет прогресс каждые CHECKPOINT_EVERY батчей.
При рестарте продолжает с последнего чекпоинта.
Каждый запуск — до MAX_RUNTIME секунд, потом graceful shutdown.

Запуск:
    python3 resumable_index.py "Стадии проекта"
    python3 resumable_index.py "Управление проектом"
"""
import json, os, sys, pickle, time, argparse, signal, shutil

# --- Ограничение потоков ---
for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "4")

import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.feature_extraction.text import TfidfVectorizer

MODEL_NAME = "intfloat/multilingual-e5-large"
BATCH_SIZE = 64
CHECKPOINT_EVERY = 10       # батчей между чекпоинтами
MAX_RUNTIME = 540           # макс. секунд на запуск (в пределах 600с песочницы)
# Базовый каталог с индексами (на сервере: /mnt/write/KISU Metro)
BASE_DIR = os.environ.get("KISU_METRO_BASE", "/mnt/write/KISU Metro")


class ResumableIndexer:
    def __init__(self, section: str, device: str = "cpu"):
        self.section = section
        self.device = device
        self.out_dir = os.path.join(BASE_DIR, section)
        self.ckpt_dir = os.path.join(self.out_dir, ".checkpoint")
        self.ckpt_meta = os.path.join(self.ckpt_dir, "meta.json")
        self.ckpt_emb = os.path.join(self.ckpt_dir, "embeddings_partial.npy")
        self.stop_requested = False

    def run(self):
        chunks_path = os.path.join(self.out_dir, "chunks_export.jsonl")
        if not os.path.exists(chunks_path):
            print(f"❌ Нет файла: {chunks_path}")
            return False

        # Загружаем чанки
        chunks_raw = []
        with open(chunks_path, encoding="utf-8") as f:
            for line in f:
                chunks_raw.append(json.loads(line))
        texts = [c["text"] for c in chunks_raw]
        N = len(texts)
        total_chars = sum(len(t) for t in texts)
        total_batches = (N + BATCH_SIZE - 1) // BATCH_SIZE

        print(f"[{time.strftime('%H:%M:%S')}] {self.section}")
        print(f"  Чанков: {N} | Батчей: {total_batches} | "
              f"Символов: {total_chars:,} | Страниц: {len(set(c.get('page_id','') for c in chunks_raw))}")

        # Проверяем чекпоинт
        start_batch = 0
        all_embeddings = None
        if os.path.exists(self.ckpt_meta):
            with open(self.ckpt_meta) as f:
                ckpt = json.load(f)
            start_batch = ckpt["processed_batches"]
            if os.path.exists(self.ckpt_emb):
                all_embeddings = np.load(self.ckpt_emb)
                print(f"  🔄 Возобновление с батча {start_batch}/{total_batches} "
                      f"(уже {all_embeddings.shape[0]} чанков)")

        if start_batch >= total_batches:
            print("  ✅ Все батчи уже обработаны, перехожу к BM25")
            return self._finish(all_embeddings, texts, chunks_raw, N)

        # Загружаем модель
        print(f"[{time.strftime('%H:%M:%S')}] Загрузка модели ({self.device})...")
        model = SentenceTransformer(MODEL_NAME, device=self.device)

        # Определяем размерность
        if all_embeddings is None:
            dim = model.get_sentence_embedding_dimension()
            all_embeddings = np.zeros((N, dim), dtype=np.float32)
        else:
            dim = all_embeddings.shape[1]
            # Расширяем до полного размера N (чекпоинт хранит только готовые)
            partial = all_embeddings
            all_embeddings = np.zeros((N, dim), dtype=np.float32)
            all_embeddings[:partial.shape[0]] = partial

        # Установка таймера
        signal.signal(signal.SIGALRM, self._timeout_handler)
        signal.alarm(MAX_RUNTIME)

        t0 = time.time()
        batches_done = 0
        try:
            for b in range(start_batch, total_batches):
                if self.stop_requested:
                    print(f"\n  ⏰ Таймаут после {batches_done} батчей, сохраняю чекпоинт...")
                    # Сохраняем прогресс перед выходом (последний завершённый батч)
                    last_batch = start_batch + batches_done
                    self._save_checkpoint(all_embeddings, last_batch, total_batches, N, dim)
                    break

                start = b * BATCH_SIZE
                end = min(start + BATCH_SIZE, N)
                batch_emb = model.encode(
                    texts[start:end],
                    show_progress_bar=False,
                    batch_size=BATCH_SIZE,
                    normalize_embeddings=True
                )
                all_embeddings[start:end] = batch_emb
                batches_done += 1

                # Чекпоинт
                if (b + 1) % CHECKPOINT_EVERY == 0 or b == total_batches - 1:
                    self._save_checkpoint(all_embeddings, b + 1, total_batches, N, dim)
                    elapsed = time.time() - t0
                    remaining = total_batches - (b + 1)
                    eta = (elapsed / (b + 1 - start_batch)) * remaining if b + 1 > start_batch else 0
                    pct = (b + 1) * 100 / total_batches
                    print(f"  💾 батч {b+1}/{total_batches} ({pct:.0f}%) | "
                          f"+{elapsed:.0f}с | ETA {eta:.0f}с")

        finally:
            signal.alarm(0)

        # Проверяем, завершено ли
        ckpt_batches = self._read_checkpoint_batches()
        if ckpt_batches >= total_batches:
            print(f"[{time.strftime('%H:%M:%S')}] Все батчи готовы, финализация...")
            return self._finish(all_embeddings, texts, chunks_raw, N)
        else:
            print(f"[{time.strftime('%H:%M:%S')}] Прогресс: {ckpt_batches}/{total_batches} батчей. "
                  f"Запустите снова для продолжения.")
            return False  # не завершено

    def _timeout_handler(self, signum, frame):
        self.stop_requested = True

    def _save_checkpoint(self, embeddings, processed_batches, total_batches, N, dim):
        os.makedirs(self.ckpt_dir, exist_ok=True)
        processed_chunks = min(processed_batches * BATCH_SIZE, N)
        np.save(self.ckpt_emb, embeddings[:processed_chunks])
        with open(self.ckpt_meta, "w") as f:
            json.dump({
                "processed_batches": processed_batches,
                "total_batches": total_batches,
                "total_chunks": N,
                "dim": dim,
                "section": self.section
            }, f)

    def _read_checkpoint_batches(self):
        if os.path.exists(self.ckpt_meta):
            with open(self.ckpt_meta) as f:
                return json.load(f).get("processed_batches", 0)
        return 0

    def _finish(self, embeddings, texts, chunks_raw, N):
        """BM25 + сохранение финального индекса."""
        embeddings = embeddings[:N]

        # BM25
        print(f"[{time.strftime('%H:%M:%S')}] BM25 индекс ({N} текстов)...")
        t0 = time.time()
        vectorizer = TfidfVectorizer(max_features=10000, lowercase=True)
        bm25_matrix = vectorizer.fit_transform(texts)
        print(f"  Готово за {time.time()-t0:.1f}с | shape={bm25_matrix.shape}")

        # Индексные чанки
        index_chunks = [
            {"chunk_id": i, "text": c["text"], "title": c["title"],
             "url": c.get("url", ""), "page_id": c.get("page_id", "")}
            for i, c in enumerate(chunks_raw)
        ]

        # Сохранение
        print(f"[{time.strftime('%H:%M:%S')}] Сохранение...")
        np.save(os.path.join(self.out_dir, "embeddings.npy"), embeddings)
        with open(os.path.join(self.out_dir, "bm25_index.pkl"), "wb") as f:
            pickle.dump({"vectorizer": vectorizer, "matrix": bm25_matrix}, f)
        with open(os.path.join(self.out_dir, "chunks_index.json"), "w", encoding="utf-8") as f:
            json.dump(index_chunks, f, ensure_ascii=False, indent=2)

        dim = int(embeddings.shape[1])
        meta = {
            "section": self.section, "total_chunks": len(index_chunks),
            "embedding_model": MODEL_NAME, "embedding_dim": dim,
            "chunk_size": 800, "chunk_overlap": 150,
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "confluence_url": "https://conf-metro.ibs.ru"
        }
        with open(os.path.join(self.out_dir, "index_meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        for fn in ["embeddings.npy", "bm25_index.pkl", "chunks_index.json"]:
            sz = os.path.getsize(os.path.join(self.out_dir, fn))
            print(f"  {fn}: {sz/1024/1024:.1f} MB")

        # Чистим чекпоинт
        if os.path.exists(self.ckpt_dir):
            shutil.rmtree(self.ckpt_dir)

        print(f"[{time.strftime('%H:%M:%S')}] ✅ {self.section} — ГОТОВО")
        return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resumable индекс для KISU Metro")
    parser.add_argument("section", help="Имя раздела")
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    args = parser.parse_args()

    indexer = ResumableIndexer(args.section, device=args.device)
    done = indexer.run()
    sys.exit(0 if done else 2)  # exit code 2 = not finished
