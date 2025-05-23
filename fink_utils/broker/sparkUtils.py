# Copyright 2019 AstroLab Software
# Author: Julien Peloton
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
from typing import Tuple
from pyspark import SparkContext
from pyspark.sql import SparkSession
from pyspark.sql import DataFrame
from pyspark.sql.column import Column, _to_java_column
from pyspark.sql.types import StructType

# import os
import json

from fink_utils.broker.avroUtils import readschemafromavrofile


def from_avro(dfcol: Column, jsonformatschema: str) -> Column:
    """Decode the Avro data contained in a DataFrame column into a struct.

    Notes
    -----
    Pyspark does not have all features contained in Spark core (Scala), hence
    we provide here a wrapper around the Scala function `from_avro`.
    You need to have the package org.apache.spark:spark-avro_2.11:2.x.y in the
    classpath to have access to it from the JVM.

    Parameters
    ----------
    dfcol: Column
        Streaming DataFrame Column with encoded Avro data (binary).
        Typically this is what comes from reading stream from Kafka.
    jsonformatschema: str
        Avro schema in JSON string format.

    Returns
    -------
    out: Column
        DataFrame Column with decoded Avro data.

    Examples
    --------
    >>> _, _, alert_schema_json = get_schemas_from_avro(ztf_avro_sample)
    >>> df_decoded = dfstream.select(
    ...   from_avro(dfstream["value"], alert_schema_json).alias("decoded"))
    >>> query = df_decoded.writeStream.queryName("qraw").format("memory")
    >>> t = query.outputMode("update").start()
    >>> t.stop()
    """
    sc = SparkContext._active_spark_context
    avro = sc._jvm.org.apache.spark.sql.avro
    f = getattr(getattr(avro, "package$"), "MODULE$").from_avro
    return Column(f(_to_java_column(dfcol), jsonformatschema))


def to_avro(dfcol: Column) -> Column:
    """Serialize the structured data of a DataFrame column into avro data (binary).

    Notes
    -----
    Since Pyspark does not have a function to convert a column to and from
    avro data, this is a wrapper around the scala function 'to_avro'.
    Just like the function above, to be able to use this you need to have
    the package org.apache.spark:spark-avro_2.11:2.x.y in the classpath.

    Parameters
    ----------
    dfcol: Column
        A DataFrame Column with Structured data

    Returns
    -------
    out: Column
        DataFrame Column encoded into avro data (binary).
        This is what is required to publish to Kafka Server for distribution.

    Examples
    --------
    >>> from pyspark.sql.functions import col, struct
    >>> avro_example_schema = '''
    ... {
    ...     "type" : "record",
    ...     "name" : "struct",
    ...     "fields" : [
    ...             {"name" : "col1", "type" : "long"},
    ...             {"name" : "col2", "type" : "string"}
    ...     ]
    ... }'''
    >>> df = spark.range(5)
    >>> df = df.select(struct("id",\
                 col("id").cast("string").alias("id2"))\
                 .alias("struct"))
    >>> avro_df = df.select(to_avro(col("struct")).alias("avro"))
    """
    sc = SparkContext._active_spark_context
    avro = sc._jvm.org.apache.spark.sql.avro
    f = getattr(getattr(avro, "package$"), "MODULE$").to_avro
    return Column(f(_to_java_column(dfcol)))


def write_to_csv(batchdf: DataFrame, batchid: int, fn: str = "web/data/simbadtype.csv"):
    """Write DataFrame data into a CSV file.

    The only supported Output Modes for File Sink is `Append`, but we need the
    complete table updated and dumped on disk here.
    Therefore this routine allows us to use CSV file sink with `Complete`
    output mode.
    TODO: that would be great to generalise this method!
    Get rid of these hardcoded paths!

    Parameters
    ----------
    batchdf: DataFrame
        Static Spark DataFrame with stream data. Expect 2 columns
        with variable names and their count.
    batchid: int
        ID of the batch.
    fn: str, optional
        Filename for storing the output.

    Examples
    --------
    >>> rdd = spark.sparkContext.parallelize(zip([1, 2, 3], [4, 5, 6]))
    >>> df = rdd.toDF(["type", "count"])
    >>> write_to_csv(df, 0, fn="test.csv")
    >>> os.remove("test.csv")
    """
    batchdf.toPandas().to_csv(fn, index=False)
    batchdf.unpersist()


def init_sparksession(
    name: str, shuffle_partitions: int = None, tz=None
) -> SparkSession:
    """Initialise SparkSession, the level of log for Spark and some configuration parameters

    Parameters
    ----------
    name: str
        Name for the Spark Application.
    shuffle_partitions: int, optional
        Number of partition to use when shuffling data.
        Typically better to keep the size of shuffles small.
        Default is None.
    tz: str, optional
        Timezone. Default is None.

    Returns
    -------
    spark: SparkSession
        Spark Session initialised.

    Examples
    --------
    >>> spark_tmp = init_sparksession("test")
    >>> conf = spark_tmp.sparkContext.getConf().getAll()
    """
    # Grab the running Spark Session,
    # otherwise create it.
    spark = SparkSession.builder.appName(name).getOrCreate()

    # keep the size of shuffles small
    if shuffle_partitions is not None:
        spark.conf.set("spark.sql.shuffle.partitions", shuffle_partitions)

    if tz is not None:
        spark.conf.set("spark.sql.session.timeZone", tz)

    # Set spark log level to WARN
    spark.sparkContext.setLogLevel("WARN")

    return spark


def get_spark_context() -> SparkContext:
    """Return the current SparkContext.

    Raise a RuntimeError if spark hasn't been initialized.

    Returns
    -------
    sparkContext : SparkContext instance
        The active sparkContext

    Examples
    --------
    >>> pysc = get_spark_context()
    >>> print(type(pysc))
    <class 'pyspark.context.SparkContext'>
    """
    if SparkContext._active_spark_context:
        return SparkContext._active_spark_context
    else:
        raise RuntimeError("SparkContext must be initialized")


def connect_to_kafka(
    servers: str,
    topic: str,
    startingoffsets: str = "latest",
    failondataloss: bool = False,
    kerberos: bool = False,
) -> DataFrame:
    """Initialise SparkSession, and set default Kafka parameters

    Parameters
    ----------
    servers: str
        kafka.bootstrap.servers as a comma-separated IP:PORT.
    topic: str
        Comma separated Kafka topic names.
    startingoffsets: str, optional
        From which offset you want to start pulling data. Options are:
        latest (only new data), earliest (connect from the oldest
        offset available), or a number (see Spark Kafka integration).
        Default is latest.
    failondataloss: bool, optional
        If True, Spark streaming job will fail if it is asking for data offsets
        that do not exist anymore in Kafka (because they have been deleted after
        exceeding a retention period for example). Default is False.
    kerberos: bool, optional
        If True, add options for a kerberized Kafka cluster. Default is False.

    Returns
    -------
    df: Streaming DataFrame
        Streaming DataFrame connected to Kafka stream

    Examples
    --------
    >>> dfstream_tmp = connect_to_kafka("localhost:29092", "ztf-stream-sim")
    >>> dfstream_tmp.isStreaming
    True
    """
    # Grab the running Spark Session
    spark = SparkSession.builder.getOrCreate()

    conf = spark.sparkContext.getConf().getAll()

    # Create a streaming DF from the incoming stream from Kafka
    df = spark.readStream.format("kafka").option("kafka.bootstrap.servers", servers)

    if kerberos:
        df = df.option(
            "kafka.sasl.kerberos.kinit.cmd",
            'kinit -t "%{sasl.kerberos.keytab}" -k %{sasl.kerberos.principal}',
        )
        df = df.option("kafka.sasl.kerberos.service.name", "kafka")

    # Naive check for secure connection - this can be improved...
    to_secure = sum(["-Djava.security.auth.login.config=" in i[1] for i in conf])  # noqa: C419
    if to_secure > 0:
        if kerberos:
            df = df.option("kafka.security.protocol", "SASL_PLAINTEXT").option(
                "kafka.sasl.mechanism", "GSSAPI"
            )
        else:
            df = df.option("kafka.sasl.mechanism", "PLAIN").option(
                "kafka.security.protocol", "SASL_SSL"
            )

    df = (
        df.option("subscribe", topic)
        .option("startingOffsets", startingoffsets)
        .option("failOnDataLoss", failondataloss)
        .load()
    )

    return df


def connect_to_raw_database(basepath: str, path: str, latestfirst: bool) -> DataFrame:
    """Initialise SparkSession, and connect to the raw database (Parquet)

    Parameters
    ----------
    basepath: str
        The base path that partition discovery should start with.
    path: str
        The path to the data (typically as basepath with a glob at the end).
    latestfirst: bool
        whether to process the latest new files first,
        useful when there is a large backlog of files

    Returns
    -------
    df: Streaming DataFrame
        Streaming DataFrame connected to the database

    Examples
    --------
    >>> dfstream_tmp = connect_to_raw_database(
    ...   "online/raw", "online/raw/*", True)
    >>> dfstream_tmp.isStreaming
    True
    """
    # Grab the running Spark Session
    spark = SparkSession.builder.getOrCreate()

    # Create a DF from the database
    userschema = spark.read.parquet(basepath).schema

    df = (
        spark.readStream.format("parquet")
        .schema(userschema)
        .option("basePath", basepath)
        .option("path", path)
        .option("latestFirst", latestfirst)
        .load()
    )

    return df


def load_parquet_files(path: str) -> DataFrame:
    """Initialise SparkSession, and load parquet files with Spark

    Unlike connect_to_raw_database, you get a standard DataFrame, and
    not a Streaming DataFrame.

    Parameters
    ----------
    path: str
        The path to the data

    Returns
    -------
    df: DataFrame
        Spark SQL DataFrame

    Examples
    --------
    >>> df = load_parquet_files(ztf_alert_sample)
    """
    # Grab the running Spark Session
    spark = SparkSession.builder.getOrCreate()

    # TODO: add mergeSchema option
    df = spark.read.format("parquet").option("mergeSchema", "true").load(path)

    return df


def get_schemas_from_avro(avro_path: str) -> Tuple[StructType, dict, str]:
    """Build schemas from an avro file (DataFrame & JSON compatibility)

    Parameters
    ----------
    avro_path: str
        Path to avro file from which schema will be extracted

    Returns
    -------
    df_schema: pyspark.sql.types.StructType
        Avro DataFrame schema
    alert_schema: dict
        Schema of the alert as a dictionary (DataFrame Style)
    alert_schema_json: str
        Schema of the alert as a string (JSON style)

    Examples
    --------
    >>> df_schema, alert_schema, alert_schema_json = get_schemas_from_avro(
    ...   ztf_avro_sample)
    >>> print(type(df_schema))
    <class 'pyspark.sql.types.StructType'>
    >>> print(type(alert_schema))
    <class 'dict'>
    >>> print(type(alert_schema_json))
    <class 'str'>
    """
    # Grab the running Spark Session
    spark = SparkSession.builder.getOrCreate()

    # Get Schema of alerts
    alert_schema = readschemafromavrofile(avro_path)
    df_schema = spark.read.format("avro").load("file://" + avro_path).schema
    alert_schema_json = json.dumps(alert_schema)

    return df_schema, alert_schema, alert_schema_json
