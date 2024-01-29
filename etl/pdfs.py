
import etl.shared
import json

def transform_papers_to_json(papers_path):
    with open(papers_path) as f:
        pdf_infos = json.load(f)

    # print(pdf_infos[:100:20])

    # E nrich the paper data by finding direct PDF URLs where we can
    paper_data = map(get_pdf_url, pdf_infos)

    # turn the PDFs into JSON documents
    it = map(extract_pdf, paper_data)
    documents = etl.shared.unchunk(it)

    # Test out debug
    #pp.pprint(documents[0]["metadata"])

    # Split document list into 10 pieces
    chunked_documents = etl.shared.chunk_into(documents, 10)
    results = list(map(etl.shared.add_to_document_db, chunked_documents))

    # Pull only arxiv other_papers
    """
    query = {"metadata.source": {"$regex": "arxiv\.org", "$options": "i"}}
    # Project out the text field, it can get large
    projection = {"text": 0}
    # get just one result to show it worked
    result = docstore.query_one(query, projection)


    pp.pprint(result)
    """


def main(json_path, collection=None, db=None):
    """Calls the ETL pipeline using a JSON file with PDF metadata.
    """
    import json
    from pathlib import Path

    json_path = Path(json_path).resolve()

    if not json_path.exists():
        print(f"{json_path} not found, writing to it from the database.")
        paper_data = fetch_papers.call()
        paper_data_json = json.dumps(paper_data, indent=2)
        with open(json_path, "w") as f:
            f.write(paper_data_json)

    with open(json_path) as f:
        paper_data = json.load(f)

    paper_data = get_pdf_url.map(paper_data, return_exceptions=True)

    documents = etl.shared.unchunk(extract_pdf.map(paper_data, return_exceptions=True))

    with etl.shared.stub.run():
        chunked_documents = etl.shared.chunk_into(documents, 10)
        list(
            etl.shared.add_to_document_db.map(
                chunked_documents, kwargs={"db": db, "collection": collection}
            )
        )

def extract_pdf(paper_data):
    """Extracts the text from a PDF and adds metadata."""
    import logging

    import arxiv

    from langchain.document_loaders import PyPDFLoader, PyPDFDirectoryLoader

    pdf_url = None
    if "folder" in paper_data:
        loader = PyPDFDirectoryLoader(paper_data["folder"])
        #docs = loader.load_and_split()
    elif "singlepdf" in paper_data:
        loader = PyPDFLoader(paper_data['singlepdf'])
    else:
        pdf_url = paper_data.get("pdf_url")
        if pdf_url is None:
            return []

        logger = logging.getLogger("pypdf")
        logger.setLevel(logging.ERROR)

        loader = PyPDFLoader(pdf_url)

    try:
        documents = loader.load_and_split()
    except Exception:
        return []

    documents = [document.dict() for document in documents]
    for document in documents:  # rename page_content to text, handle non-unicode data
        document["text"] = (
            document["page_content"].encode("utf-8", errors="replace").decode()
        )
        document.pop("page_content")

    if pdf_url is not None:
        if "arxiv" in pdf_url:
            arxiv_id = extract_arxiv_id_from_url(pdf_url)
            # create an arXiV database client with a 5 second delay between requests
            client = arxiv.Client(page_size=1, delay_seconds=5, num_retries=5)
            # describe a search of arXiV's database
            search_query = arxiv.Search(id_list=[arxiv_id], max_results=1)
            try:
                # execute the search with the client and get the first result
                result = next(client.results(search_query))
            except ConnectionResetError as e:
                raise Exception("Triggered request limit on arxiv.org, retrying") from e
            metadata = {
                "arxiv_id": arxiv_id,
                "title": result.title,
                "date": result.updated,
            }
        else:
            metadata = {"title": paper_data.get("title")}
    else:
        metadata = {"title": "notknown"}

    documents = annotate_endmatter(documents)

    for document in documents:
        document["metadata"]["source"] = paper_data.get("url", pdf_url)
        document["metadata"] |= metadata
        title, page = (
            document["metadata"]["title"],
            document["metadata"]["page"],
        )
        if title:
            document["metadata"]["full-title"] = f"{title} - p{page}"

    documents = etl.shared.enrich_metadata(documents)

    return documents

def concatenate_content_multiple_pages(documents, ignore_endmatter=True):
    return "".join(doc['text'] for doc in documents if doc['metadata'].get("is_endmatter", False) is False)

#@stub.function()
def fetch_papers(collection_name="all-content"):
    """Fetches other_papers from the LLM Lit Review, https://tfs.ai/llm-lit-review."""
    import docstore

    client = docstore.connect()

    collection = client.get_database("llm-lit-review").get_collection(collection_name)

    # Query to retrieve documents with the "PDF?" field set to true
    query = {"properties.PDF?.checkbox": {"$exists": True, "$eq": True}}

    # Projection to include the "Name", "url", and "Tags" fields
    projection = {
        "properties.Name.title.plain_text": 1,
        "properties.Link.url": 1,
        "properties.Tags.multi_select.name": 1,
    }

    # Fetch documents matching the query and projection
    documents = list(collection.find(query, projection))
    assert documents

    papers = []
    for doc in documents:
        paper = {}
        paper["title"] = doc["properties"]["Name"]["title"][0]["plain_text"]
        paper["url"] = doc["properties"]["Link"]["url"]
        paper["tags"] = [
            tag["name"]
            for tag in doc.get("properties", {}).get("Tags", {}).get("multi_select", [])
        ]
        papers.append(paper)

    assert papers

    return papers


#@stub.function()
def get_pdf_url(paper_data):

    if "folder" in paper_data:
        return paper_data

    """Attempts to extract a PDF URL from a paper's URL."""
    url = paper_data["url"]
    if url.strip("#/").endswith(".pdf"):
        pdf_url = url
    elif "arxiv.org" in url:
        arxiv_id = extract_arxiv_id_from_url(url)
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    elif "aclanthology.org" in url:
        pdf_url = url.strip("/")
        url += ".pdf"
    else:
        pdf_url = None
    paper_data["pdf_url"] = pdf_url

    return paper_data


def annotate_endmatter(pages, min_pages=3):
    """Heuristic for detecting reference sections."""
    out, after_references = [], False
    for idx, page in enumerate(pages):

        # Starting a new chunk ? Then remove after_references
        if after_references is True and ('metadata' in page and 'page' in page['metadata']):
            if page['metadata']['page'] == 0:
                after_references = False

        content = page["text"].lower()
        if idx >= min_pages and ("references" in content or "bibliography" in content):
            after_references = True
        page["metadata"]["is_endmatter"] = after_references
        out.append(page)
    return out


def extract_arxiv_id_from_url(url):
    import re

    # pattern = r"(?:arxiv\.org/abs/|arxiv\.org/pdf/)(\d{4}\.\d{4,5}(?:v\d+)?)"
    match_arxiv_url = r"(?:arxiv\.org/abs/|arxiv\.org/pdf/)"
    match_id = r"(\d{4}\.\d{4,5}(?:v\d+)?)"  # 4 digits, a dot, and 4 or 5 digits
    optional_version = r"(?:v\d+)?"

    pattern = match_arxiv_url + match_id + optional_version

    match = re.search(pattern, url)
    if match:
        return match.group(1)
    else:
        return None
