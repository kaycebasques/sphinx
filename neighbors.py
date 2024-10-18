import json


import numpy as np
from sklearn.metrics.pairwise import cosine_similarity


def find_docname(data, target):
    for docname in data:
        if data[docname]['embedding'] == target:
            return docname
    return None


# https://web.archive.org/web/20240923001107/https://docs.voyageai.com/docs/quickstart-tutorial
def k_nearest_neighbors(target, embeddings, k=5):
    # Convert to numpy array
    target = np.array(target)
    embeddings = np.array(embeddings)
    # Reshape the query vector embedding to a matrix of shape (1, n) to make it 
    # compatible with cosine_similarity
    target = target.reshape(1, -1)
    # Calculate the similarity for each item in data
    cosine_sim = cosine_similarity(target, embeddings)
    # Sort the data by similarity in descending order and take the top k items
    sorted_indices = np.argsort(cosine_sim[0])[::-1]
    # Take the top k related embeddings
    top_k_related_embeddings = embeddings[sorted_indices[:k]]
    top_k_related_embeddings = [
        list(row[:]) for row in top_k_related_embeddings
    ]  # convert to list
    return top_k_related_embeddings


with open('doc/embeddings.json', 'r') as f:
    data = json.load(f)
embeddings = [data[docname]['embedding'] for docname in data]
for target in embeddings:
    dot_products = np.dot(embeddings, target)
    neighbors = k_nearest_neighbors(target, embeddings, k=3)
    # ignore neighbors[0] because that is always the target itself
    nearest_neighbor = neighbors[1]
    target_docname = find_docname(data, target)
    neighbor_docname = find_docname(data, nearest_neighbor)
    print(target_docname, neighbor_docname)
