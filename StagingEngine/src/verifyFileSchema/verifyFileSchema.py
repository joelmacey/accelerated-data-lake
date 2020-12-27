import csv
import json
import re
import traceback
import io

import boto3
from jsonschema import validate
from jsonschema.exceptions import ValidationError

import pandas as pd
import csvvalidator


class VerifyFileSchemaException(Exception):
    pass


s3 = boto3.resource('s3')


def lambda_handler(event, context):
    '''
    lambda_handler Top level lambda handler ensuring all exceptions
    are caught and logged.

    :param event: AWS Lambda uses this to pass in event data.
    :type event: Python type - Dict / list / int / string / float / None
    :param context: AWS Lambda uses this to pass in runtime information.
    :type context: LambdaContext
    :return: The event object passed into the method
    :rtype: Python type - Dict / list / int / string / float / None
    :raises VerifyFileSchemaException: On any error or exception
    '''
    try:
        return _verify_file_schema(event, context)
    except VerifyFileSchemaException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise VerifyFileSchemaException(e)


def _verify_file_schema(event, context):
    '''
    verify_file_schema Verifies the schema of the new file if schema
    and format information has been added to the data source config.

    :param event: AWS Lambda uses this to pass in event data.
    :type event: Python type - Dict / list / int / string / float / None
    :param context: AWS Lambda uses this to pass in runtime information.
    :type context: LambdaContext
    :return: The event object passed into the method
    :rtype: Python type - Dict / list / int / string / float / None
    :raises VerifyFileSchemaException: When insufficient config information
    '''
    bucket = event['fileDetails']['bucket']
    key = event['fileDetails']['key']
    file_settings = event['fileSettings']
    file_type = event["fileType"]
    
    if 'schema' in event and event['schema'] is not None:
        if 'fileFormat' in file_settings:
            file_content = _load_object_content(bucket, key)
            if file_settings['fileFormat'] == 'json':
                _verify_json_schema(file_content, event['schema'])
            elif file_settings['fileFormat'] == 'csv':
                _verify_csv_schema(file_content, ',', event['schema'])
            elif file_settings['fileFormat'] == 'tsv':
                _verify_csv_schema(file_content, '\t', event['schema'])
            else:
                raise VerifyFileSchemaException(
                    "Filetype: {} has a defined schema but no "
                    " file format specified".format(file_type))
        else:
            print("Filetype: {} has no defined fileFormat so no "
                  " verification will take place.".format(file_type))
    elif 'glue' in event and event['glue'] is not None:
        # Retrieve data types from the file
        s3_key = f's3://{bucket}/{key}'

        if file_settings['fileFormat'] == 'csv':
            separator = ','
        elif file_settings['fileFormat'] == 'tsv':
            separator = '\t'
        else:
            separator = ',' # default to comma
        df = pd.read_csv(s3_key,sep=separator,header=0)
        csv_datatype_list = []
        for dtype in df.dtypes.iteritems():
            if dtype[1] == 'object':
                csvdtype = 'string'
            elif dtype[1] == 'int64':
                csvdtype = 'int'
            elif dtype[1] == 'float64':
                csvdtype = 'float'
            elif dtype[1] == 'bool':
                csvdtype = 'boolean'
            elif dtype[1] == 'datetime64':
                csvdtype = 'date'
            else:
                csvdtype = None
            csv_datatype_list.append(csvdtype)
        
        # Retrieve data types from the glue table
        glue_datatype_list = []
        response = _get_glue_table(event['glue']['database'],event['glue']['table'])
        for i in response['Table']['StorageDescriptor']['Columns']:
            if i['Type'] == 'int' or i['Type'] == 'bigint' or i['Type'] == 'smallint' or i['Type'] == 'tinyint' or i['Type'] == 'binary':
                gluedtype = 'int'
            elif i['Type'] == 'boolean':
                gluedtype = 'boolean'
            elif i['Type'] == 'char' or i['Type'] == 'string' or i['Type'] == 'varchar' or i['Type'] == 'interval':
                gluedtype = 'string'
            elif i['Type'] == 'date' or i['Type'] == 'timestamp':
                gluedtype = 'date'
            elif i['Type'] == 'decimal' or i['Type'] == 'double' or i['Type'] == 'float':
                gluedtype = 'float'
            else:
                gluedtype = None
            glue_datatype_list.append(gluedtype)
        for i in range(0, len(glue_datatype_list)):
            if glue_datatype_list[i] == csv_datatype_list[i]:
                pass
            elif glue_datatype_list[i] != csv_datatype_list[i] and glue_datatype_list[i] == 'string': # Overwrite to allow values to equal strings
                pass
            else:
                raise VerifyFileSchemaException(
                    "Data type in the csv for entry {} is equal to {} expected {}".format(i,csv_datatype_list[i], glue_datatype_list[i]))
    else:
        print("Filetype: {} has no defined schema so no "
              " verification will take place.".format(file_type))

    return event


def _verify_json_schema(file_content, schema):
    '''
    _verify_json_schema Verifies the schema of json data. The while loop
    is present to allow json documents batched into the same file by firehose
    to be processed and verified.

    :param file_content: The content of the file
    :type file_content: Python String
    :param schema: The jsonschema we are expecting
    :type schema: Python String
    :raises Exception: When file_content schema is incorrect
    '''
    decoder = json.JSONDecoder()
    start_position = 0
    while True:
        match = re.search('[{\[]', file_content[start_position:])
        if not match:
            break
        start_position = match.start() + start_position

        json_object, end_position = decoder.raw_decode(
            file_content[start_position:])

        try:
            validate(json_object, schema)
        except ValidationError as ve:
            raise VerifyFileSchemaException(ve.message[:10240])

        start_position = start_position + end_position


def _verify_csv_schema(file_content, separator, schema):
    '''
    _verify_csv_schema Verifies the schema of csv data. Only required
    column names are confirmed

    :param file_content: The content of the file
    :type file_content: Python String
    :param separator: The delimeter character used in the file
    :type separator: Python Character
    :param schema: The csv schema we are expecting
    :type schema: Python String
    :raises Exception: When file_content schema is incorrect
    '''
    file_content_lines = file_content.splitlines()
    csv_reader = csv.reader(file_content_lines, delimiter=separator)

    field_names = []
    schema_properties = schema['properties']
    for prop in schema_properties:
        field_names.append(prop['field'])

    # field_names = tuple(schema['properties'])

    validator = csvvalidator.CSVValidator(tuple(field_names))
    validator.add_header_check('EX1', 'bad header')

    for prop in schema_properties:
        prop_field = prop['field']
        prop_type = prop['type']
        if prop_type == 'int':
            validator.add_value_check(prop_field, int, 'EX_INT', prop_field + ' must be an integer')
        elif prop_type == 'string':
            validator.add_value_check(prop_field, str, 'EX_STR', prop_field + ' must be a string')
        elif prop_type == 'enum':
            enum_values = tuple(prop['values'])
            validator.add_value_check(prop_field, csvvalidator.enumeration(enum_values), 'EX_ENUM', prop_field + ' must have value from enum')

    problems = validator.validate(csv_reader)

    if len(problems) > 0:
        raise VerifyFileSchemaException(str(problems))


def _load_object_content(bucket, key):
    '''
    load_object_content Loads the given object (identified by
    bucket and key) from S3

    :param bucket:  The S3 bucket name
    :type bucket: Python String
    :param key: The S3 object key
    :type key: Python String
    :return: Contents of S3 object as a string
    :rtype: Python String
    '''
    s3_object = s3.Object(bucket, key)
    return s3_object.get()["Body"].read().decode('utf-8')

def _get_glue_table(database, table):
    client = boto3.client('glue')
    
    response = client.get_table(
        DatabaseName=database,
        Name=table
    )

    return response