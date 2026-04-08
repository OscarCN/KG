we are dealing with developing a commercial grade system where entities are to be linked to ground truth entities in a database, consider:

an entity linking system has 3 main components:
knowledge base (kb)
retrieval system
disambiguation or linking system

mantaining a proper entities knowledge base:
	- building a database that uniquely and consistently identifies entities
	- extending the database
	- enforcing object types and schemas with an ontology, 

the task of linking:
	- retrieval: whether searching by name (efficiently retrieving similar names), by location (geographic coordinates and features), or by a combination of those types of features
	- matching: deciding on the right entity from a set of candidates, especially when using trained machine learning models on features that can be: 
		* derived from language: from text that uniquely and consistently define an entity, such as using its description (e.g. "john morgan is a basketball player in the jaysons since 2015"),
		* derived from location information, i.e. identifying a location by its address or coordinates
		* derived from other features, such time, taxonomies, identifiers (that can be available or not even in the same ontology class)



every entity should be grounded in a real, identifiable, unmistakable thing. consider the case of uniquely describing:
a person: name and a description which can be textual (john heinz, president of the chamber of commerce of california since 2016)
an event: depending on its type it can be uniquely identified by its time (datetime or datetime range), type of event (new law announcement, car crash), and whether its location, or/and its description.
a real estate development: address or location, type (apartments building or single apparment, hotel, etc.) and/or its name, if it has one
amongst others

We will build an entity linking system to match events that occur in a time and place (accidents, protests, concerts, congresses, and general events, classified with broad, enforceable taxonomies), entities that exist geographically (real estate developments, infrastructure projects, locations, etc.), people, products, technologies, companies, regulations, and general entities, according to well defined taxonomies.


we will find references to entities and their (partial) descriptions from different kinds of sources (news and social media content, semi structured databases, websites, contracts, marketplaces, etc.) and we want to link them to a ground truth entity which exists in a knowledge base, or



knowledge base structure

the kb has an entities component and an ontology component.

entities:

entities follow two conventions, 

geographic entities which have their own linking system and kb structure (geographic entities such as cities, streets, venues, etc)
general entities, events and entities (such as person, organization, concert, festival, car accident, industry, product, technology)


geographic/locations kb
the ground truth geographic knowledge base consists of a hierarchy of geographic entities: countries, provinces, cities or municipalities, neighborhoods, streets, places (such as a place with address or a place known by name, such as a stadium, a restaurant, etc.)
geographic relations between entities define a tree, where each location has a parent according to the "is in" relation (a province is in a country, a street is in a neighborhood, etc.)

entities/events kb
the ground truth entities knowledge base consist of a general set of entities that have a type (according to a taxonomy or ontology), can have a name, location, time, numeric and other types of features, which depend on its type (taxonomy).

Events follow the same structure as general entities, they always have a date or date range, and can be heavily reliant on taxonomy information or location and/or on an accurate description, such as "announcement of a new regulation on property rental policies in the state of arizona" or "publication of 2025 Q3 filing report of NVDA" 
from now on, we will refer to events also as entities

entities have an ontology class that defines their type (car accident, concert, company, technology, ...)


ontologies:
every entity type has an ontology class.

object (entity) schemas are defined and enforced by their ontology class, schemas consider all attributes it might have, ontology classes also define how entities are uniquely described (e.g. a car accident is described by time and location. A real estate development is described by location, and/or name, if it has one, etc...)

ontology classes are well thought-off, designed, and won't hold relations.

Ontologies define what objects should be extracted and their schema, extraction of structured data is done via LLM calls. Prompts for extracting structured data according to the schema should be automatically generated using the schema and descriptions


knowledge bases will be extended continuously from unstructured, semi structured, and structured data, such as creating entities and features from news, etc., or incorporating new structured datasets.

each data type is always structured the same way, e.g. a name, a datetime, a datetime range, a description, a price (including, e.g. currency), an url, a list, a list of urls, a location (city, street, place,...), etc. Each of these have a specific type class and parser

ontologies define schemas and identifying features 


we are architecting a modular, object oriented software system in python that should handle:


Parsing ontologies from json files (see schema/ directory and schema/readme_schema.py)

using ontologies to create prompts for retrieving structured entities data from news and unstructured content

calling LLMs to structure the data according to our schema

Parsing LLM output using the Parser class in schema/




knowledge base data access, for kb storage and entity retrieval: 
    (In early stages of development we will do this naively and locally)

	-includes indices for efficient retrieval, such as
		*geographic (shape contains point, nearby or closest coordinates, implemented in postgresql)
		*textual similarity: 
			**lexical: locality sensitive hashing, implemented in redis. useful for retrieving names by similarity (places, people, products)
			**embeddings: cosine or euclidean distance using embeddings on descriptions, narratives and question answering techniques, implemented in a vector database (postgresql?)
	
	-kb and indices include aliases for each entity: each entity has one to many aliases

	-the type of retrieval for every entity is defined by its taxonomy (whether its an event or entity, and its event type or entity type), and usually uses many features (the same features that describe the type of entity), it can use textual similarity (names), time, location (an event for which you know the city can be the same as one for which you know the street, which is in the same city).

	-should handle adding and removing entities, adding aliases (to indices and kb), 
	
	-postgresql is used for kb

database and indices should be synchronized, indices provide fast, easy access to essential information of the knowledge bases (redis includes id, coordinates, name, parent, geographic shape, stored as a json), and the kb stores ground truth information and other data.









entity linking or disambiguation

geographic linking: non matched locations are defined by how they are referred to (mentions), each mention refers to a type of location entity (a mention "ciudad de mexico" referring to a city, "avenida reforma" referring to a street, etc.)
e.g.
[{'position_in_text': 1,
  'level': 1,
  'mention_id': 0,
  'context_group': 1,
  'text': 'México',
  'std_text': 'mexic',
  'confidence': 0.6},
 {'position_in_text': 3,
  'level': 3,
  'mention_id': 1,
  'context_group': 1,
  'text': 'León',
  'std_text': 'leon',
  'confidence': 0.6},
 {'position_in_text': 6,
  'level': 6,
  'mention_id': 2,
  'context_group': 1,
  'text': 'Bulevar Macheteros',
  'std_text': 'macheter',
  'confidence': 0.6}]

geographic linking considers 	

















