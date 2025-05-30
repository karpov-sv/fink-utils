# Copyright 2019 AstroLab Software
# Author: Abhishek Chauhan, Julien Peloton
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json
import os
import glob
import shutil
import time

from fink_utils.broker.avroUtils import readschemafromavrofile
from fink_utils.broker.sparkUtils import to_avro, from_avro

from pyspark.sql import DataFrame
from pyspark.sql.functions import struct, lit
from pyspark.sql.avro.functions import to_avro as to_avro_native


def get_kafka_df(
    df: DataFrame,
    schema_path: str,
    fink_broker_version: str,
    fink_science_version: str,
    saveschema: bool = False,
    elasticc: bool = False,
) -> DataFrame:
    """Create and return a df to pubish to Kafka

    For a kafka output the dataframe should have the following columns:
    key: (optional) Using a unique key can prevent reading duplicate data
        as Kafka supports "at least once" write semantics
        and might result in duplicate writing
    value: (required)
    topic: (*optional) if a topic field is not present in the dataframe
        it has to be given while writing to kafka
    This routine groups the DataFrame columns to be published to Kafka into
    a StructType column and convert it into avro(binary).
    To be able to decode the sent Kafka messages, the avro schema is stored
    at schema_path which is passed as an argument.

    Parameters
    ----------
    df: DataFrame
        A Spark DataFrame created after reading the science database (HBase)
    schema_path: str
        Path where to store the avro schema required for decoding the
        Kafka messages.
    saveschema: bool
        If True, save the alert schema on disk. Work only in Spark local mode,
        and for testing purposes. Default is False.

    Returns
    -------
    df: DataFrame
        A Spark DataFrame with an avro(binary) encoded Column named "value"
    """
    # Remove the status column before distribution
    cols = df.columns
    if "status" in cols:
        cols.remove("status")

    df = df.select(cols)

    # Create a StructType column in the df for distribution.
    # The contents and schema of the df can change over time
    df_struct = df.select(struct(df.columns).alias("struct"))

    # Convert into avro and save the schema
    if elasticc:
        # The idea is to force the output schema
        # Need better handling of this though...
        jsonschema = open(
            "/home/julien.peloton/elasticc/alert_schema/elasticc.v0_9.brokerClassification.avsc",
            "r",
        ).read()
        df_kafka = df_struct.select(to_avro_native("struct", jsonschema).alias("value"))
    else:
        df_kafka = df_struct.select(to_avro("struct").alias("value"))

    # Add a key based on schema versions
    df_kafka = df_kafka.withColumn(
        "key", lit("{}_{}".format(fink_broker_version, fink_science_version))
    )

    if saveschema:
        # Harcoded path that corresponds to the schema used
        # for alert redistribution.
        schema_path = "schemas/distribution_schema_new.avsc"

        # Do not work on a DFS like HDFS obviously.
        # Only local mode & for testing purposes
        toto = df.writeStream.foreachBatch(
            lambda x, y: save_avro_schema_stream(x, y, schema_path, False)
        ).start()
        time.sleep(10)

        # Note that the entire Spark application will stop.
        toto.stop()

    return df_kafka


def save_avro_schema_stream(
    df: DataFrame, epochid: int, schema_path=None, save_on_hdfs: bool = False
):
    """Extract schema from an alert of the stream, and save it on disk.

    Mostly for debugging purposes - do not work in cluster mode (local only).
    Typically:
    schema_path = ...
    toto = df_stream.writeStream.foreachBatch(
        lambda x, y: save_avro_schema_stream(x, y, schema_path)
    ).start()
    time.sleep(10)
    toto.stop()

    Parameters
    ----------
    df: DataFrame
        Micro-batch Dataframe containing alerts
    epochid: int
        Offset of the micro-batch
    """
    save_avro_schema(df, schema_path, save_on_hdfs)


def save_avro_schema(df: DataFrame, schema_path: str, save_on_hdfs: bool = False):
    """Writes the avro schema to a file at schema_path

    This routine checks if an avro schema file exist at the given path
    and creates one if it doesn't.
    To automatically change the schema with changing requirements, ensure to
    delete the schema file at the given path whenever the structure of DF
    read from science db or the contents to be distributed are changed.

    Parameters
    ----------
    df: DataFrame
        A Spark DataFrame
    schema_path: str
        Path where to store the avro schema
    """
    # Check if the file exists
    if not os.path.isfile(schema_path):
        # Store the df as an avro file
        if save_on_hdfs:
            prefix_path = "hdfs:///"
        else:
            prefix_path = "file:///"
        path_for_avro = os.path.join(
            prefix_path, os.environ["PWD"][1:], "flatten_hbase.avro"
        )
        if os.path.exists(path_for_avro):
            shutil.rmtree(path_for_avro)
        df.write.format("avro").save(path_for_avro)

        # Read the avro schema from .avro file
        if save_on_hdfs:
            avro_file = glob.glob(path_for_avro + "/.part*")[0]
        else:
            avro_file = glob.glob(
                os.path.join(os.environ["PWD"], "flatten_hbase.avro", ".part*")
            )[0]
        avro_schema = readschemafromavrofile(avro_file)

        # Write the schema to a file for decoding Kafka messages
        with open(schema_path, "w") as f:
            json.dump(avro_schema, f, indent=2)

        # Remove .avro files and directory
        shutil.rmtree(path_for_avro)
    else:
        msg = """
            {} already exists - cannot write the new schema
        """.format(schema_path)
        print(msg)


def decode_kafka_df(df_kafka: DataFrame, schema_path: str) -> DataFrame:
    """Decode the DataFrame read from Kafka

    The DataFrame read from Kafka contains the following columns:
    key: binary
    value: binary
    topic: string
    partition: int
    offset: long
    timestamp: long
    timestampType: integer
    The value column contains the structured data of the alert encoded into
    avro(binary). This routine creates a Spark DataFrame with a decoded
    StructType column using the avro schema at schema_path.

    Parameters
    ----------
    df_kafka: DataFrame
        A Spark DataFrame created after reading the Kafka Source
    schema_path: str
        Path where the avro schema to decode the Kafka message is stored

    Returns
    -------
    df: DataFrame
        A Spark DataFrame with a StructType Column with decoded data of
        the avro(binary) column named "value"

    Examples
    --------
    # >>> from pyspark.sql.functions import col
    # >>> df = spark.sparkContext.parallelize(zip(
    # ...     ["ZTF18aceatkx", "ZTF18acsbjvw"],
    # ...     [697251923115015002, 697251921215010004],
    # ...     [20.393772, 20.4233877],
    # ...     [-25.4669463, -27.0588511],
    # ...     ["Star", "Unknown"])).toDF([
    # ...       "objectId", "candid", "candidate_ra",
    # ...       "candidate_dec", "cross_match_alerts_per_batch"])
    # >>> df.show()
    # +------------+------------------+------------+-------------+----------------------------+
    # |    objectId|            candid|candidate_ra|candidate_dec|cross_match_alerts_per_batch|
    # +------------+------------------+------------+-------------+----------------------------+
    # |ZTF18aceatkx|697251923115015002|   20.393772|  -25.4669463|                        Star|
    # |ZTF18acsbjvw|697251921215010004|  20.4233877|  -27.0588511|                     Unknown|
    # +------------+------------------+------------+-------------+----------------------------+
    # <BLANKLINE>
    # >>> temp_schema = os.path.join("file:///", os.environ["PWD"][1:], "temp_schema")
    # >>> save_avro_schema(df, temp_schema)
    # >>> df_kafka = get_kafka_df(df, '', "2.2", "2.0.0") # Encode the data into avro
    # >>> df_decoded = decode_kafka_df(df_kafka, temp_schema) # Decode the avro df
    # >>> df_decoded.printSchema()
    # root
    #  |-- struct: struct (nullable = true)
    #  |    |-- objectId: string (nullable = true)
    #  |    |-- candid: long (nullable = true)
    #  |    |-- candidate_ra: double (nullable = true)
    #  |    |-- candidate_dec: double (nullable = true)
    #  |    |-- cross_match_alerts_per_batch: string (nullable = true)
    # <BLANKLINE>
    # >>> df_decoded.select(col("struct.*")).show()
    # +------------+------------------+------------+-------------+----------------------------+
    # |    objectId|            candid|candidate_ra|candidate_dec|cross_match_alerts_per_batch|
    # +------------+------------------+------------+-------------+----------------------------+
    # |ZTF18aceatkx|697251923115015002|   20.393772|  -25.4669463|                        Star|
    # |ZTF18acsbjvw|697251921215010004|  20.4233877|  -27.0588511|                     Unknown|
    # +------------+------------------+------------+-------------+----------------------------+
    # <BLANKLINE>
    # >>> os.remove(temp_schema)
    """
    # Read the avro schema
    with open(schema_path) as f:
        avro_schema = json.dumps(json.load(f))

    # Decode the avro(binary) column
    df = df_kafka.select(from_avro("value", avro_schema).alias("struct"))

    return df


def get_distribution_offset(
    offsetfile: str, startingoffset_dist: str = "latest"
) -> int:
    """Read and return distribution offset from file

    Parameters
    ----------
    offsetfile: str
        the path of offset file for distribution
    startingoffset_dist: str, optional
        Offset(timestamp) from where to start the distribution. Options are
        latest (read timestamp from file), earliest (from the beginning of time),
        timestamp (custom timestamp input by user)

    Returns
    -------
    timestamp: int
        a timestamp (typical unix timestamp: time in ms since epoch)

    Examples
    --------
    # set a test timestamp
    >>> test_t = int(round(time.time() * 1000))
    >>> with open('dist.offset.test', 'w') as f: # write to a file
    ...     string = "distributed till, {}".format(test_t)
    ...     f.write(string)
    ...
    31
    >>> min_timestamp = get_distribution_offset("invalidFile", "latest") # test 1 (wrong file name)
    >>> min_timestamp
    100
    >>> min_timestamp = get_distribution_offset("dist.offset.test", "earliest") # test 2 (given earliest)
    >>> min_timestamp
    100
    >>> min_timestamp = get_distribution_offset("dist.offset.test", "latest") # test 3 (given latest)
    >>> min_timestamp == test_t
    True
    >>> min_timestamp = get_distribution_offset("dist.offset.test") # test 4 (no offset given)
    >>> min_timestamp == test_t
    True
    >>> custom_t = int(round(time.time() * 1000)) # test 5 (custom timestamp given)
    >>> min_timestamp = get_distribution_offset("dist.offset.test", custom_t)
    >>> min_timestamp == custom_t
    True
    >>> os.remove('dist.offset.test') # Delete offset file
    """
    # if the offset file doesn't exist or is empty or
    # one wants the earliest offset
    fileexist = os.path.isfile(offsetfile)
    if fileexist:
        negatifoffset = os.path.getsize(offsetfile) <= 0
    else:
        negatifoffset = False
    if not fileexist or negatifoffset or startingoffset_dist == "earliest":
        # set a default
        min_timestamp = 100
    else:
        if startingoffset_dist == "latest":
            with open(offsetfile, "r") as f:
                line = f.readlines()[-1]
                min_timestamp = int(line.split(", ")[-1])
        else:
            # user given timestamp
            min_timestamp = int(startingoffset_dist)

    return min_timestamp


def group_df_into_struct(df: DataFrame, colfamily: str, key: str) -> DataFrame:
    """Group columns of a df into a struct column

    Notes
    -----
    Currently, the dataframe is transformed by splitting it into
    two dataframes, reshaping one of them and then using a join.
    This might consume more resources than necessary and should be
    optimized in the future if required.
    If we have a df with the following schema:
    root
     |-- objectId: string (nullable = true)
     |-- candidate_ra: double (nullable = true)
     |-- candidate_dec: double (nullable = true)
    and we want to group all `candidate_*` into a struct like:
    root
     |-- objectId: string (nullable = true)
     |-- candidate: struct (nullable = false)
     |    |-- ra: double (nullable = true)
     |    |-- dec: double (nullable = true)

    Parameters
    ----------
    df: Spark DataFrame
        a Spark dataframe with flat columns
    colfamily: str
        prefix of columns to be grouped into a struct
    key: str
        a column with unique values (used for join)

    Returns
    -------
    df: Spark DataFrame
        a Spark dataframe with columns grouped into struct

    Examples
    --------
    >>> df = spark.sparkContext.parallelize(zip(
    ...     ["ZTF18aceatkx", "ZTF18acsbjvw"],
    ...     [697251923115015002, 697251921215010004],
    ...     [20.393772, 20.4233877],
    ...     [-25.4669463, -27.0588511],
    ...     ["Star", "Unknown"])).toDF([
    ...       "objectId", "candid", "candidate_ra",
    ...       "candidate_dec", "cross_match_alerts_per_batch"])
    >>> df.printSchema()
    root
     |-- objectId: string (nullable = true)
     |-- candid: long (nullable = true)
     |-- candidate_ra: double (nullable = true)
     |-- candidate_dec: double (nullable = true)
     |-- cross_match_alerts_per_batch: string (nullable = true)
    <BLANKLINE>
    >>> df = group_df_into_struct(df, 'candidate', 'objectId')
    >>> df.printSchema()
    root
     |-- objectId: string (nullable = true)
     |-- candid: long (nullable = true)
     |-- cross_match_alerts_per_batch: string (nullable = true)
     |-- candidate: struct (nullable = false)
     |    |-- ra: double (nullable = true)
     |    |-- dec: double (nullable = true)
    <BLANKLINE>
    """
    struct_cols = []
    flat_cols = []

    pos = len(colfamily) + 1

    for col_ in df.columns:
        if col_.startswith(colfamily + "_"):
            struct_cols.append(col_)
        else:
            flat_cols.append(col_)

    # dataframe with columns other than 'columnFamily_*'
    df1 = df.select(flat_cols)

    new_col_names = []
    new_col_names.append(key)

    # dataframe with key + 'columnFamily_*'
    df2 = df.select(new_col_names + struct_cols)

    struct_cols = [x[pos:] for x in struct_cols]

    new_col_names.extend(struct_cols)
    df2_renamed = df2.toDF(*new_col_names)

    # Group 'columnFamily_*' into a struct
    df2_struct = df2_renamed.select(key, struct(*struct_cols).alias(colfamily))

    # join the two dataframes based on 'key'
    df_new = df1.join(df2_struct, key)

    return df_new


def write_to_kafka(
    sdf,
    key,
    kafka_bootstrap_servers,
    kafka_sasl_username,
    kafka_sasl_password,
    topic_name,
    checkpoint_stream_path,
    processingTime,
):
    """
    Send data to a Kafka cluster using Apache Spark

    Parameters
    ----------
    sdf: Spark DataFrame
        DataFrame
    key: str
        key for each Avro message
    kafka_bootstrap_servers: str
        Comma-separated list of ip:port of the Kafka machines
    kafka_sasl_username: str
        Username for writing into the Kafka cluster
    kafka_sasl_password: str
        Password for writing into the Kafka cluster
    topic_name: str
        Kafka topic (does not need to exist)
    checkpoint_stream_path: str
        path where to store the stream checkpoint files
    processingTime: float
        processing interval time between batch
    Return
    ------
    disquery: Spark Streaming DataFrame
        Streaming DataFrame
    """
    # Create a StructType column in the df for distribution.
    df_struct = sdf.select(struct(sdf.columns).alias("struct"))
    df_kafka = df_struct.select(to_avro("struct").alias("value"))
    df_kafka = df_kafka.withColumn("key", key)

    # Send schema
    disquery = (
        df_kafka.writeStream.format("kafka")
        .option("kafka.bootstrap.servers", kafka_bootstrap_servers)
        .option("kafka.sasl.username", kafka_sasl_username)
        .option("kafka.sasl.password", kafka_sasl_password)
        .option("topic", topic_name)
        .option("checkpointLocation", checkpoint_stream_path)
        .trigger(processingTime="{} seconds".format(processingTime))
        .start()
    )

    return disquery
