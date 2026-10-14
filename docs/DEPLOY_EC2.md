# One EC2 instance, one big disk, S3 for files

Yes, this runs on a single EC2 instance: Qdrant and the application on one large EBS volume, files in S3, and PostgreSQL either on
the same volume (simplest) or in RDS (recommended). It is a **single machine**: if the instance or its availability zone is lost,
search is down until you restore from snapshots (hours, not seconds). That is a fair trade up to a few hundred million chunks; past
that, or if you need no downtime, see `docs/scale/CAPACITY.md` and `docker-compose.cluster.yml`.

## 1. Size it from your data, not from the disk

A 5 TB disk is not the limit; **memory** is. Qdrant keeps a compressed copy of every vector, the graph links and (by default) the keyword
index in RAM; the full-precision vectors live on the disk. Count your extracted text (not file sizes), then run the planner:

    cd backend && python -m scripts.scale_capacity --text-gb 100 --dim 384 --sparse-on-disk --payload-on-disk --node-ram-gb 128

Rough guide (arithmetic, not measured on your data): about 1.1 KB of text per chunk, about 1.1 KB of RAM per chunk with the
settings in `.env.prod.example`, and about 3 KB of disk per chunk at 384 dimensions (5 KB at 1,024). So 50 million chunks is about 55 GB of RAM and
150 GB of disk: a 5 TB volume is far more than that index needs, and is more likely to hold Postgres and growth. Pick the instance by RAM
(memory-optimised `r6i` / `r7i`), leave 40 % free for the operating system, the application and the page cache.

Ingest speed is set by the embedding model: about 1.5 documents per second per 2-CPU machine with a local CPU model (measured). A million
documents is a week or more on one box; use more worker processes (`SCALE_WORKER_CONCURRENCY`, `--scale worker=N`) or a GPU embedding server.

## 2. The disk

* One **gp3** volume (up to 16 TiB). gp3 gives 3,000 IOPS and 125 MB/s by default and lets you provision up to 16,000 IOPS and 1,000 MB/s: with vectors on disk, search is random-read heavy, so provision IOPS (start around 12,000) and check your instance type's own EBS limit. These figures are AWS's at the time of writing; confirm them.
* Mount it at `/data` and create the three directories:

      sudo mkfs.xfs /dev/nvme1n1 && sudo mkdir /data && sudo mount /dev/nvme1n1 /data
      echo '/dev/nvme1n1 /data xfs defaults,nofail 0 2' | sudo tee -a /etc/fstab     # use the UUID in real life
      sudo mkdir -p /data/postgres /data/qdrant /data/rag

* In `.env.prod` point the containers at it (otherwise Docker puts them under `/var/lib/docker` on the small root disk):

      PGDATA_DIR=/data/postgres
      QDRANT_DATA_DIR=/data/qdrant
      RAG_DATA_DIR=/data/rag

## 3. Files in S3, with an instance role

Create a private bucket, then attach an IAM role to the instance with this policy (replace `my-bucket`) and leave `S3_ACCESS_KEY` /
`S3_SECRET_KEY` empty in `.env.prod`. Nothing secret is then stored on the machine.

    { "Version": "2012-10-17", "Statement": [
      { "Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": "arn:aws:s3:::my-bucket" },
      { "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], "Resource": "arn:aws:s3:::my-bucket/*" } ] }

Set `S3_BUCKET`, `S3_REGION`, and `STORAGE_PUBLIC_URL` (the bucket's https address; it lets the browser load previews). Add a CORS rule on the bucket for your site's origin (GET).
Connect sources that live in S3 (a different bucket) from Sources > S3 bucket; that is separate from this file store.

## 4. PostgreSQL: on the disk or in RDS

* **On the disk:** the compose file already runs it. Fine, but a Postgres container on one machine has no point-in-time recovery of its own.
* **RDS (recommended):** automated backups and point-in-time recovery for free. Create a PostgreSQL 16 instance in the same VPC, put its address in `POSTGRES_DSN`, and in your copy of `docker-compose.prod.yml` delete the `postgres` service and the `depends_on: postgres` entries. Allow the instance's security group in RDS's. Use `sslmode=require` in the DSN.

## 5. Bring it up

    bash scripts/get-models.sh                     # the local embedding and reranking models, into ./models
    cp .env.prod.example .env.prod                 # fill it in (docs/CONFIGURATION.md lists every other setting)
    bash scripts/prod/start.sh                     # builds, runs migrations, starts API, workers, Qdrant, nginx
    bash scripts/cli.sh prod status                # workers alive? index healthy?

Open only 80/443 in the security group (22 from your address). Qdrant and Postgres are not published outside the Docker network.

## 6. Backups (the part people skip)

| what | how | why |
|---|---|---|
| the whole `/data` volume | EBS snapshots on a schedule (Data Lifecycle Manager) | the Qdrant index is *derived* from Postgres and S3, but rebuilding a large one takes days; a snapshot brings it back in minutes |
| Postgres | RDS backups, or `bash scripts/backup.sh prod ./backups` for a dump | the one store that cannot be rebuilt |
| S3 bucket | versioning on | the originals |

Snapshots of a running Postgres/Qdrant volume are crash-consistent, which both engines recover from; for a clean copy, stop the stack or use the engines' own snapshot commands. **Restore a backup to a clean machine once before you rely on it.**

If the index is lost or old, `bash scripts/cli.sh prod reconcile --force` re-embeds whatever is missing from the stored pages, and Health shows the progress.

## 7. Moving a model or growing later

* A new embedding model never takes search down: the old index answers until the new one has caught up (`docs/scale/CAPACITY.md`, section 5).
* To go beyond one machine, the Qdrant data can be moved into the three-node cluster file; Postgres moves to RDS at any time; workers scale out on more instances against the same Postgres and S3.
