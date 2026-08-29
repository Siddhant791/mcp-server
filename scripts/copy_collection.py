import argparse

from pymongo import MongoClient, UpdateOne


MONGO_URI = "mongodb+srv://Wedding:{Pwd}@wedding.emrolht.mongodb.net/?appName=Wedding"
DB_NAME = "mcp_server"


def copy_collection(copy_from: str, copy_to: str) -> None:
    client = MongoClient(MONGO_URI, tlsAllowInvalidCertificates=True)
    db = client[DB_NAME]

    source = db[copy_from]
    dest = db[copy_to]

    docs = list(source.find({}))
    if not docs:
        print(f"Source collection '{copy_from}' is empty. Nothing to copy.")
        client.close()
        return

    ops = [
        UpdateOne({"_id": doc["_id"]}, {"$set": doc}, upsert=True)
        for doc in docs
    ]
    result = dest.bulk_write(ops, ordered=False)

    print(
        f"Copied {len(docs)} documents from '{copy_from}' to '{copy_to}'. "
        f"Inserted: {result.upserted_count}, Updated: {result.modified_count}."
    )
    client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy all documents from one MongoDB collection to another."
    )
    parser.add_argument("copy_from", help="Source collection name")
    parser.add_argument("copy_to", help="Destination collection name")
    args = parser.parse_args()

    copy_collection(args.copy_from, args.copy_to)


if __name__ == "__main__":
    main()
