TODO:

[OK] see e.g. the News Schema field "author": {"type": list}. What changes do we need to do for fields like this to be defined instead as List[str] using python typing and recursively parse nested types (str in this case). Same case as UrlList for List[Url]


Currently, when running Parser.parse_object_structure objects are parsed by traversing object keys and arranging keys that should be in nested objects, so if e.g. the schema 
is {'date_range': {'start':datetime, 'end':datetime}} a valid input object is {'start':datetime, 'end':datetime}, 
this will be problematic when different nested objects share key names. Change the Parser.parse_object_structure function to be able to work with prefixed keys,
in the example, if the prefixed_keys argument flag is true (default false), and prefix_char_separator (default '_'), it will parse
{'date_range_start':datetime, 'date_range_end':datetime} into {'date_range': {'start':datetime, 'end':datetime}}
at each level of nesting, match the destination schema key names with the input object keys using startswith to avoid problems with the prefix_char_separator being present in key names


[OK] Separate data types and parsers in types.py into different files in a new directory types/ for semantically similar data types (dates, primitives, strings (url), (to be added: currency, location, events))
[OK] create a type_catalog.py file in types/ with all implemented data types and their file — see types/type_catalog.py for the full registry of implemented types, their parser classes, and source files
[OK] update readme_schema.md, point to the type_catalog.py


[OK] Change the schemas to be defined by a json instead of python dictionaries and types, map types as strings in the json to python types using a types mapping (only for python )
[OK] create a new file schema/schemas/read_schema.py to translate a given json or json file path into the corresponding dictionaries.

[OK] add a 'meta' key to the schema definition for each object type, with an optional description field 
[OK] e.g. DateRangeFromUnstructured:
[OK] {'schema': {'date_range': PeriodDates, 'timezone': str, 'mention': str, 'precision_days': int},
'description': "Represents a date period extracted from unstructured text, with a date start and end, optional timezone, the original unstructured mention e.g. 'durante la segunda semana de enero', and the precision confidence period in days (the amount of uncertainty we have given the unstructured mention)"
}


We will define many data types that will be reusable across schemas, e.g. the DateRangeFromUnstructured can be used for the hypothetical schema of "Cierre de calle o carretera" field "periodo_cierre" and in the schema of "Festival" field "fecha_festival". what considerations should we consider for making them reusable and mantainable? e.g. namespaces, where to save schemas, etc. We can start saving schemas in local json files and referencing them from a catalogue, later we can save them in a database, e.g. postgres