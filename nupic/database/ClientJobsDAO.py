#!/usr/bin/env python
# ----------------------------------------------------------------------
# Numenta Platform for Intelligent Computing (NuPIC)
# Copyright (C) 2013, Numenta, Inc.  Unless you have an agreement
# with Numenta, Inc., for a separate license for this software code, the
# following terms and conditions apply:
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see http://www.gnu.org/licenses.
#
# http://numenta.org/licenses/
# ----------------------------------------------------------------------

# Add Context Manager (with ...) support for Jython/Python 2.5.x (
# ClientJobManager used to use Jython); it's a noop in newer Python versions.
from __future__ import with_statement

import collections
import logging
from optparse import OptionParser
import sys
import traceback
import uuid

from nupic.support.decorators import logExceptions #, logEntryExit
import pymysql
from pymysql.constants import ER as mysqlerrors

from nupic.database.Connection import ConnectionFactory
from nupic.support.configuration import Configuration
from nupic.support import pymysqlhelpers



_MODULE_NAME = "nupic.database.ClientJobsDAO"


###############################################################################
class InvalidConnectionException(Exception):
  """ This exception is raised when a worker tries to update a model record that
  belongs to another worker. Ownership of a model is determined by the database
  connection id
  """
  pass


###############################################################################
def _getLogger():
  """ NOTE: this cannot be a global variable because the logging subsystem
  needs to be initialized by the host app, and that usually happens after
  imports
  """
  return logging.getLogger(
    ".".join(['com.numenta', _MODULE_NAME, ClientJobsDAO.__name__]))


# Create a decorator for retrying idempotent SQL operations upon transient MySQL
#  failures.
# WARNING: do NOT indiscriminately decorate non-idempotent operations with this
#  decorator as it #  may case undesirable side-effects, such as multiple row
#  insertions, etc.
# NOTE: having this as a global permits us to switch parameters wholesale (e.g.,
#  timeout)
g_retrySQL = pymysqlhelpers.retrySQL(getLoggerCallback=_getLogger)



###############################################################################
def _abbreviate(text, threshold):
  """ Abbreviate the given text to threshold chars and append an ellipsis if its
  length exceeds threshold; used for logging;

  NOTE: the resulting text could be longer than threshold due to the ellipsis
  """
  if text is not None and len(text) > threshold:
    text = text[:threshold] + "..."

  return text



##############################################################################
# The ClientJobsDAO class
##############################################################################
class ClientJobsDAO(object):
  """ This Data Access Object (DAO) is used for creating, managing, and updating
  the ClientJobs database. The ClientJobs database is a MySQL database shared by
  the UI, Stream Manager (StreamMgr), and the engine. The clients (UI and
  StreamMgr) make calls to this DAO to request new jobs (Hypersearch, stream
  jobs, model evaluations, etc.) and the engine queries and updates it to manage
  and keep track of the jobs and report progress and results back to the
  clients.

  This class is primarily a collection of static methods that work with the
  client jobs database. But, rather than taking the approach of declaring each
  method as static, we provide just one static class method that returns a
  reference to the (one) ClientJobsDAO instance allocated for the current
  process (and perhaps in the future, for the current thread). This approach
  gives us the flexibility in the future of perhaps allocating one instance per
  thread and makes the internal design a bit more compartmentalized (by enabling
  the use of instance variables). Note: This is generally referred to as
  the singleton pattern.

  A typical call is made in the following manner:
    ClientJobsDAO.get().jobInfo()

  If the caller desires, they have the option of caching the instance returned
  from ClientJobsDAO.get(), i.e.:
    cjDAO = ClientJobsDAO.get()
    cjDAO.jobInfo()
    cjDAO.jobSetStatus(...)

  There are two tables in this database, the jobs table and the models table, as
  described below. The jobs table keeps track of all jobs. The models table is
  filled in by hypersearch jobs with the results of each model that it
  evaluates.

  Jobs table. The field names are given as:
      internal mysql field name (public API field name)

  field     description
  ---------------------------------------------------------------------------
  job_id (jobId): Generated by the database when a new job is inserted by a
            client. This is an auto-incrementing ID that is unique among all
            jobs.

  client (client): The name of the client (i.e. 'UI', 'StreamMgr', etc.).

  client_info (clientInfo): Arbitrary data specified by client.

  client_key (clientKey): Foreign key as defined by the client.

  cmd_line (cmdLine): Command line to be used to launch each worker process for
            the job.

  params (params):   JSON encoded dict of job specific parameters that are
            useful to the worker processes for this job. This field is provided
            by the client when it inserts the job and can be fetched out of the
            database by worker processes (based on job_id) if needed.

  job_hash (jobHash): hash of the job, provided by the client, used for
            detecting identical jobs when they use the jobInsertUnique() call.
            Clients that don't care about whether jobs are unique or not do not
            have to generate or care about this field.

  status (status):   The engine will periodically update the status field as the
            job runs.
            This is an enum. Possible values are:
              STATUS_NOTSTARTED   client has just added this job to the table
              STATUS_STARTING:    a CJM is in the process of launching this job in the
                                   engine
              STATUS_RUNNING:     the engine is currently running this job
              STATUS_TESTMODE:    the job is being run by the test framework
                                    outside the context of hadoop, should be
                                    ignored
              STATUS_COMPLETED:   the job has completed. The completion_reason
                                    field describes the manner in which it
                                    completed

  completion_reason (completionReason): Why this job completed.  Possible values
            are:
              CMPL_REASON_SUCCESS:  job completed successfully
              CMPL_REASON_KILLED:   job was killed by ClientJobManager
              CMPL_REASON_CANCELLED:  job was cancelled by user
              CMPL_REASON_ERROR:    job encountered an error. The completion_msg
                                  field contains a text description of the error

  completion_msg (completionMsg): Text description of error that occurred if job
            terminated with completion_reason of CMPL_REASON_ERROR or
            CMPL_REASON_KILLED

  worker_completion_msg (workerCompletionMsg): Why this job completed, according
            to the worker(s).

  cancel (cancel):   Set by the clent if/when it wants to cancel a job.
            Periodically polled by the CJM and used as a signal to kill the job.
            TODO: the above claim doesn't match current reality: presently,
                  Hypersearch and Production workers poll the cancel field.

  start_time (startTime): date and time of when this job started.

  end_time (endTime): date and time of when this job completed.

  results (results): A JSON encoded dict of the results of a hypersearch job.
                    The dict contains the following fields. Note that this dict
                    is NULL before any model has reportedits results:

              bestModel: The modelID of the best performing model so far
              bestValue: The value of the optimized metric for the best model

  _eng_last_update_time (engLastUpdateTime):  Time stamp of last update. Used
            for detecting stalled jobs.

  _eng_cjm_conn_id (engCjmConnId):  The database client connection ID of the CJM
            (Client Job Manager) starting up this job. Set and checked while the
            job is in the 'starting' phase. Used for detecting and dealing with
            stalled CJM's

  _eng_worker_state (engWorkerState): JSON encoded data structure
            for private use by the workers.

  _eng_status (engStatus): String used to send status messages from the engine
            to the UI. For informative purposes only.

  _eng_model_milestones (engModelMilestones): JSON encoded object with
            information about global model milestone results.

  minimum_workers (minimumWorkers): min number of desired workers at a time.
            If 0, no workers will be allocated in a crunch

  maximum_workers (maximumWorkers): max number of desired workers at a time. If
            0, then use as many as practical given load on the cluster.

  priority (priority): job scheduling priority; 0 is the default priority (
            ClientJobsDAO.DEFAULT_JOB_PRIORITY); positive values are higher
            priority (up to ClientJobsDAO.MAX_JOB_PRIORITY), and negative values
            are lower priority (down to ClientJobsDAO.MIN_JOB_PRIORITY)

  _eng_allocate_new_workers (engAllocateNewWorkers): Should the scheduling
            algorithm allocate new workers to this job? If a specialized worker
            willingly gives up control, we set this field to FALSE to avoid
            allocating new workers.

  _eng_untended_dead_workers (engUntendedDeadWorkers): If a specialized worker
            fails or is killed by the scheduler, we set this feild to TRUE to
            indicate that the worker is dead.

  num_failed_workers (numFailedWorkers): The number of failed specialized workers
           for this job. if the number of failures is greater than
           max.failed.attempts, we mark the job as failed

  last_failed_worker_error_msg (lastFailedWorkerErrorMsg): Error message of the
           most recent failed specialized worker


  Models table: field     description
  ---------------------------------------------------------------------------
  model_id (modelId):  Generated by the database when the engine inserts a new
              model. This is an auto-incrementing ID that is globally unique
              among all models of all jobs.

  job_id (jobId) : The job_id of the job in the Jobs Table that this model
              belongs to.

  params (params):    JSON encoded dict of all the parameters used to generate
            this particular model. The dict contains the following properties:
                  paramValues     = modelParamValuesDict,
                  paramLabels     = modelParamValueLabelsDict,
                  experimentName  = expName

  status (status):    Enumeration of the model's status. Possible values are:
                STATUS_NOTSTARTED: This model's parameters have been chosen, but
                                    no worker is evaluating it yet.
                STATUS_RUNNING:    This model is currently being evaluated by a
                                    worker
                STATUS_COMPLETED:  This model has finished running. The
                                    completion_reason field describes why it
                                    completed.

  completion_reason (completionReason) : Why this model completed.  Possible
            values are:
              CMPL_REASON_EOF:      model reached the end of the dataset
              CMPL_REASON_STOPPED:  model stopped because it reached maturity
                                      and was not deemed the best model.
              CMPL_REASON_KILLED:   model was killed by the terminator logic
                                      before maturing and before reaching EOF
                                      because it was doing so poorly
              CMPL_REASON_ERROR:    model encountered an error. The completion_msg
                                      field contains a text description of the
                                      error

  completion_msg (completionMsg): Text description of error that occurred if
            model terminated with completion_reason of CMPL_REASON_ERROR or
            CMPL_REASON_KILLED

  results (results):  JSON encoded structure containing the latest online
            metrics produced by the model. The engine periodically updates this
            as the model runs.

  optimized_metric(optimizedMetric): The value of the metric over which
          this model is being optimized. Stroring this separately in the database
          allows us to search through to find the best metric faster

  update_counter (updateCounter): Incremented by the UI whenever the engine
            updates the results field. This makes it easier and faster for the
            UI to determine which models have changed results.

  num_records (numRecords):  Number of records (from the original dataset,
            before aggregation) that have been processed so far by this model.
            Periodically updated by the engine as the model is evaluated.

  start_time (startTime): Date and time of when this model started being
            evaluated.

  end_time (endTime): Date and time of when this model completed.

  cpu_time (cpuTime): How much actual CPU time was spent evaluating this
            model (in seconds). This excludes any time the process spent
            sleeping, or otherwise not executing code.

  model_checkpoint_id (modelCheckpointId): Checkpoint identifier for this model
            (after it has been saved)

  _eng_params_hash (engParamsHash): MD5 hash of the params. Used for detecting
            duplicate models.

  _eng_particle_hash (engParticleHash): MD5 hash of the model's particle (for
            particle swarm optimization algorithm).

  _eng_last_update_time (engLastUpdateTime):  Time stamp of last update. Used
            for detecting stalled workers.

  _eng_task_tracker_id (engTaskTrackerId):  ID of the Hadoop Task Tracker
            managing the worker

  _eng_worker_id (engWorkerId): ID of the Hadoop Map Task (worker) for this task

  _eng_attempt_id (engAttemptId):  Hadoop attempt ID of this task attempt

  _eng_worker_conn_id (engWorkerConnId): database client connection ID of the
            hypersearch worker that is running this model

  _eng_milestones (engMilestones): JSON encoded list of metric values for the
            model at each milestone point.

  _eng_stop (engStop): One of the STOP_REASON_XXX enumerated value strings
            (or None). This gets set to STOP_REASON_KILLED if the terminator
            decides that the performance of this model is so poor that it
            should be terminated immediately. This gets set to STOP_REASON_STOPPED
            if Hypersearch decides that the search is over and this model
            doesn't have to run anymore.

  _eng_matured (engMatured): Set by the model maturity checker when it decides
            that this model has "matured".

  """

  # Job priority range values.
  #
  # Higher-priority jobs will be scheduled to run at the expense of the
  # lower-priority jobs, and higher-priority job tasks will preempt those with
  # lower priority if there is inadequate supply of scheduling slots. Excess
  # lower priority job tasks will starve as long as slot demand exceeds supply.
  MIN_JOB_PRIORITY = -100           # Minimum job scheduling priority
  DEFAULT_JOB_PRIORITY = 0          # Default job scheduling priority
  MAX_JOB_PRIORITY = 100            # Maximum job scheduling priority

  # Equates for job and model status
  STATUS_NOTSTARTED = "notStarted"
  STATUS_STARTING = "starting"
  STATUS_RUNNING = "running"
  STATUS_TESTMODE = "testMode"
  STATUS_COMPLETED = "completed"

  # Equates for job and model completion_reason field
  CMPL_REASON_SUCCESS = "success"    # jobs only - job completed successfully
  CMPL_REASON_CANCELLED = "cancel"   # jobs only - canceled by user;
                                     # TODO: presently, no one seems to set the
                                     #  CANCELLED reason
  CMPL_REASON_KILLED = "killed"      # jobs or models - model killed by
                                     #  terminator for poor results or job
                                     #  killed by ClientJobManager
  CMPL_REASON_ERROR = "error"        # jobs or models - Encountered an error
                                     #  while running
  CMPL_REASON_EOF = "eof"            # models only - model reached end of
                                     #  data set
  CMPL_REASON_STOPPED = "stopped"    # models only - model stopped running
                                     #  because it matured and was not deemed
                                     #  the best model.
  CMPL_REASON_ORPHAN = "orphan"      # models only - model was detected as an
                                     #  orphan because the worker running it
                                     #  failed to update the last_update_time.
                                     #  This model is considered dead and a new
                                     #  one may be created to take its place.


  # Equates for the model _eng_stop field
  STOP_REASON_KILLED = "killed"      # killed by model terminator for poor
                                     # results before it matured.
  STOP_REASON_STOPPED = "stopped"    # stopped because it had matured and was
                                     # not deemed the best model

  # Equates for the cleaned field
  CLEAN_NOT_DONE = "notdone"      # Cleaning for job is not done
  CLEAN_DONE = "done"             # Cleaning for job is done

  # Equates for standard job classes
  JOB_TYPE_HS = "hypersearch"
  JOB_TYPE_PM = "production-model"
  JOB_TYPE_SM = "stream-manager"
  JOB_TYPE_TEST = "test"

  HASH_MAX_LEN = 16
  """ max size, in bytes, of the hash used for model and job identification """

  CLIENT_MAX_LEN = 8
  """ max size, in bytes of the 'client' field's value """


  class _TableInfoBase(object):
    """ Common table info fields; base class """
    __slots__ = ("tableName", "dbFieldNames", "publicFieldNames",
                 "pubToDBNameDict", "dbToPubNameDict",)

    def __init__(self):
      self.tableName = None
      """ Database-qualified table name (databasename.tablename) """

      self.dbFieldNames = None
      """ Names of fields in schema """

      self.publicFieldNames = None
      """ Public names of fields generated programmatically: e.g.,
      word1_word2_word3  => word1Word2Word3 """

      self.pubToDBNameDict = None
      self.dbToPubNameDict = None
      """ These dicts convert public field names to DB names and vice versa """

  class _JobsTableInfo(_TableInfoBase):
    __slots__ = ("jobInfoNamedTuple",)

    # The namedtuple classes that we use to return information from various
    #  functions
    jobDemandNamedTuple = collections.namedtuple(
      '_jobDemandNamedTuple',
      ['jobId', 'minimumWorkers', 'maximumWorkers', 'priority',
       'engAllocateNewWorkers', 'engUntendedDeadWorkers', 'numFailedWorkers',
       'engJobType'])

    def __init__(self):
      super(ClientJobsDAO._JobsTableInfo, self).__init__()

      # Generated dynamically after introspecting jobs table columns. Attributes
      # of this namedtuple are the public names of the jobs table columns.
      self.jobInfoNamedTuple = None

  class _ModelsTableInfo(_TableInfoBase):
    __slots__ = ("modelInfoNamedTuple",)

    # The namedtuple classes that we use to return information from various
    #  functions
    getParamsNamedTuple = collections.namedtuple(
      '_modelsGetParamsNamedTuple', ['modelId', 'params', 'engParamsHash'])

    getResultAndStatusNamedTuple = collections.namedtuple(
      '_modelsGetResultAndStatusNamedTuple',
      ['modelId', 'results', 'status', 'updateCounter', 'numRecords',
       'completionReason', 'completionMsg', 'engParamsHash', 'engMatured'])

    getUpdateCountersNamedTuple = collections.namedtuple(
      '_modelsGetUpdateCountersNamedTuple', ['modelId', 'updateCounter'])

    def __init__(self):
      super(ClientJobsDAO._ModelsTableInfo, self).__init__()

      # Generated dynamically after introspecting models columns. Attributes
      # of this namedtuple are the public names of the models table columns.
      self.modelInfoNamedTuple = None


  _SEQUENCE_TYPES = (list, set, tuple)
  """ Sequence types that we accept in args """

  # There is one instance of the ClientJobsDAO per process. This class static
  #  variable gets filled in the first time the process calls
  # ClientJobsDAO.get()
  _instance = None


  # The root name and version of the database. The actual database name is
  #  something of the form "client_jobs_v2_suffix".
  _DB_ROOT_NAME = 'client_jobs'
  _DB_VERSION = 29

  ##############################################################################
  @classmethod
  def dbNamePrefix(cls):
    """ Get the beginning part of the database name for the current version
    of the database. This, concatenated with
    '_' + Configuration.get('nupic.cluster.database.nameSuffix') will
    produce the actual database name used.
    """
    return cls.__getDBNamePrefixForVersion(cls._DB_VERSION)


  ##############################################################################
  @classmethod
  def __getDBNamePrefixForVersion(cls, dbVersion):
    """ Get the beginning part of the database name for the given database
    version. This, concatenated with
    '_' + Configuration.get('nupic.cluster.database.nameSuffix') will
    produce the actual database name used.

    Parameters:
    ----------------------------------------------------------------
    dbVersion:      ClientJobs database version number

    retval:         the ClientJobs database name prefix for the given DB version
    """
    return '%s_v%d' % (cls._DB_ROOT_NAME, dbVersion)


  ##############################################################################
  @classmethod
  def _getDBName(cls):
    """ Generates the ClientJobs database name for the current version of the
    database; "semi-private" class method for use by friends of the class.

    Parameters:
    ----------------------------------------------------------------
    retval:         the ClientJobs database name
    """

    return cls.__getDBNameForVersion(cls._DB_VERSION)


  ##############################################################################
  @classmethod
  def __getDBNameForVersion(cls, dbVersion):
    """ Generates the ClientJobs database name for the given version of the
    database

    Parameters:
    ----------------------------------------------------------------
    dbVersion:      ClientJobs database version number

    retval:         the ClientJobs database name for the given DB version
    """

    # DB Name prefix for the given version
    prefix = cls.__getDBNamePrefixForVersion(dbVersion)

    # DB Name suffix
    suffix = Configuration.get('nupic.cluster.database.nameSuffix')

    # Replace dash with underscore (dash will break SQL e.g. 'ec2-user')
    suffix = suffix.replace("-", "_")

    # Create the name of the database for the given DB version
    dbName = '%s_%s' % (prefix, suffix)

    return dbName


  ##############################################################################
  @staticmethod
  @logExceptions(_getLogger)
  def get():
    """ Get the instance of the ClientJobsDAO created for this process (or
    perhaps at some point in the future, for this thread).

    Parameters:
    ----------------------------------------------------------------
    retval:  instance of ClientJobsDAO
    """

    # Instantiate if needed
    if ClientJobsDAO._instance is None:
      cjDAO = ClientJobsDAO()
      cjDAO.connect()

      ClientJobsDAO._instance = cjDAO

    # Return the instance to the caller
    return ClientJobsDAO._instance



  ##############################################################################
  @logExceptions(_getLogger)
  def __init__(self):
    """ Instantiate a ClientJobsDAO instance.

    Parameters:
    ----------------------------------------------------------------
    """

    self._logger = _getLogger()

    # Usage error to instantiate more than 1 instance per process
    assert (ClientJobsDAO._instance is None)

    # Create the name of the current version database
    self.dbName = self._getDBName()

    # NOTE: we set the table names here; the rest of the table info is set when
    #  the tables are initialized during connect()
    self._jobs = self._JobsTableInfo()
    self._jobs.tableName = '%s.jobs' % (self.dbName)

    self._models = self._ModelsTableInfo()
    self._models.tableName = '%s.models' % (self.dbName)

    # Our connection ID, filled in during connect()
    self._connectionID = None


  @property
  def jobsTableName(self):
    return self._jobs.tableName

  @property
  def modelsTableName(self):
    return self._models.tableName


  ##############################################################################
  def _columnNameDBToPublic(self, dbName):
    """ Convert a database internal column name to a public name. This
    takes something of the form word1_word2_word3 and converts it to:
    word1Word2Word3. If the db field name starts with '_', it is stripped out
    so that the name is compatible with collections.namedtuple.
    for example: _word1_word2_word3 => word1Word2Word3

    Parameters:
    --------------------------------------------------------------
    dbName:      database internal field name
    retval:      public name
    """

    words = dbName.split('_')
    if dbName.startswith('_'):
      words = words[1:]
    pubWords = [words[0]]
    for word in words[1:]:
      pubWords.append(word[0].upper() + word[1:])

    return ''.join(pubWords)


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def connect(self, deleteOldVersions=False, recreate=False):
    """ Locate the current version of the jobs DB or create a new one, and
    optionally delete old versions laying around. If desired, this method
    can be called at any time to re-create the tables from scratch, delete
    old versions of the database, etc.

    Parameters:
    ----------------------------------------------------------------
    deleteOldVersions:   if true, delete any old versions of the DB left
                          on the server
    recreate:            if true, recreate the database from scratch even
                          if it already exists.
    """

    # Initialize tables, if needed
    with ConnectionFactory.get() as conn:
      # Initialize tables
      self._initTables(cursor=conn.cursor, deleteOldVersions=deleteOldVersions,
                       recreate=recreate)

      # Save our connection id
      conn.cursor.execute('SELECT CONNECTION_ID()')
      self._connectionID = conn.cursor.fetchall()[0][0]
      self._logger.info("clientJobsConnectionID=%r", self._connectionID)

    return


  @logExceptions(_getLogger)
  def _initTables(self, cursor, deleteOldVersions, recreate):
    """ Initialize tables, if needed

    Parameters:
    ----------------------------------------------------------------
    cursor:              SQL cursor
    deleteOldVersions:   if true, delete any old versions of the DB left
                          on the server
    recreate:            if true, recreate the database from scratch even
                          if it already exists.
    """

    # Delete old versions if they exist
    if deleteOldVersions:
      self._logger.info(
        "Dropping old versions of client_jobs DB; called from: %r",
        traceback.format_stack())
      for i in range(self._DB_VERSION):
        cursor.execute('DROP DATABASE IF EXISTS %s' %
                              (self.__getDBNameForVersion(i),))

    # Create the database if necessary
    if recreate:
      self._logger.info(
        "Dropping client_jobs DB %r; called from: %r",
        self.dbName, traceback.format_stack())
      cursor.execute('DROP DATABASE IF EXISTS %s' % (self.dbName))

    cursor.execute('CREATE DATABASE IF NOT EXISTS %s' % (self.dbName))


    # Get the list of tables
    cursor.execute('SHOW TABLES IN %s' % (self.dbName))
    output = cursor.fetchall()
    tableNames = [x[0] for x in output]

    # ------------------------------------------------------------------------
    # Create the jobs table if it doesn't exist
    # Fields that start with '_eng' are intended for private use by the engine
    #  and should not be used by the UI
    if 'jobs' not in tableNames:
      self._logger.info("Creating table %r", self.jobsTableName)
      fields = [
        'job_id                  INT UNSIGNED NOT NULL AUTO_INCREMENT',
            # unique jobID
        'client                  CHAR(%d)' % (self.CLIENT_MAX_LEN),
            # name of client (UI, StrmMgr, etc.)
        'client_info             LONGTEXT',
            # Arbitrary data defined by the client
        'client_key             varchar(255)',
            # Foreign key as defined by the client.
        'cmd_line                LONGTEXT',
            # command line to use to launch each worker process
        'params                  LONGTEXT',
            # JSON encoded params for the job, for use by the worker processes
        'job_hash                BINARY(%d) DEFAULT NULL' % (self.HASH_MAX_LEN),
            # unique hash of the job, provided by the client. Used for detecting
            # identical job requests from the same client when they use the
            # jobInsertUnique() method.
        'status                  VARCHAR(16) DEFAULT "notStarted"',
            # One of the STATUS_XXX enumerated value strings
        'completion_reason       VARCHAR(16)',
            # One of the CMPL_REASON_XXX enumerated value strings.
            # NOTE: This is the job completion reason according to the hadoop
            # job-tracker. A success here does not necessarily mean the
            # workers were "happy" with the job. To see if the workers
            # failed, check the worker_completion_reason
        'completion_msg          LONGTEXT',
            # Why this job completed, according to job-tracker
        'worker_completion_reason   VARCHAR(16) DEFAULT "%s"'  % \
                  self.CMPL_REASON_SUCCESS,
            # One of the CMPL_REASON_XXX enumerated value strings. This is
            # may be changed to CMPL_REASON_ERROR if any workers encounter
            # an error while running the job.
        'worker_completion_msg   LONGTEXT',
            # Why this job completed, according to workers. If
            # worker_completion_reason is set to CMPL_REASON_ERROR, this will
            # contain the error information.
        'cancel                  BOOLEAN DEFAULT FALSE',
            # set by UI, polled by engine
        'start_time              DATETIME DEFAULT 0',
            # When job started
        'end_time                DATETIME DEFAULT 0',
            # When job ended
        'results                 LONGTEXT',
            # JSON dict with general information about the results of the job,
            # including the ID and value of the best model
            # TODO: different semantics for results field of ProductionJob
        '_eng_job_type           VARCHAR(32)',
            # String used to specify the type of job that this is. Current
            # choices are hypersearch, production worker, or stream worker
        'minimum_workers         INT UNSIGNED DEFAULT 0',
            # min number of desired workers at a time. If 0, no workers will be
            # allocated in a crunch
        'maximum_workers         INT UNSIGNED DEFAULT 0',
            # max number of desired workers at a time. If 0, then use as many
            # as practical given load on the cluster.
        'priority                 INT DEFAULT %d' % self.DEFAULT_JOB_PRIORITY,
            # job scheduling priority; 0 is the default priority (
            # ClientJobsDAO.DEFAULT_JOB_PRIORITY); positive values are higher
            # priority (up to ClientJobsDAO.MAX_JOB_PRIORITY), and negative
            # values are lower priority (down to ClientJobsDAO.MIN_JOB_PRIORITY)
        '_eng_allocate_new_workers    BOOLEAN DEFAULT TRUE',
            # Should the scheduling algorithm allocate new workers to this job?
            # If a specialized worker willingly gives up control, we set this
            # field to FALSE to avoid allocating new workers.
        '_eng_untended_dead_workers   BOOLEAN DEFAULT FALSE',
            # If a specialized worker fails or is killed by the scheduler, we
            # set this feild to TRUE to indicate that the worker is dead
        'num_failed_workers           INT UNSIGNED DEFAULT 0',
            # The number of failed specialized workers for this job. If the
            # number of failures is >= max.failed.attempts, we mark the job
            # as failed
        'last_failed_worker_error_msg  LONGTEXT',
            # Error message of the most recent specialized failed worker
        '_eng_cleaning_status          VARCHAR(16) DEFAULT "%s"'  % \
                  self.CLEAN_NOT_DONE,
            # Has the job been garbage collected, this includes removing
            # unneeded # model output caches, s3 checkpoints.
        'gen_base_description    LONGTEXT',
            # The contents of the generated description.py file from hypersearch
            # requests. This is generated by the Hypersearch workers and stored
            # here for reference, debugging, and development purposes.
        'gen_permutations        LONGTEXT',
            # The contents of the generated permutations.py file from
            # hypersearch requests. This is generated by the Hypersearch workers
            # and stored here for reference, debugging, and development
            # purposes.
        '_eng_last_update_time   DATETIME DEFAULT 0',
            # time stamp of last update, used for detecting stalled jobs
        '_eng_cjm_conn_id        INT UNSIGNED',
            # ID of the CJM starting up this job
        '_eng_worker_state       LONGTEXT',
            # JSON encoded state of the hypersearch in progress, for private
            # use by the Hypersearch workers
        '_eng_status             LONGTEXT',
            # String used for status messages sent from the engine for
            # informative purposes only. Usually printed periodically by
            # clients watching a job progress.
        '_eng_model_milestones   LONGTEXT',
            # JSon encoded object with information about global model milestone
            # results

        'PRIMARY KEY (job_id)',
        'UNIQUE INDEX (client, job_hash)',
        'INDEX (status)',
        'INDEX (client_key)'
        ]
      options = [
        'AUTO_INCREMENT=1000',
        ]

      query = 'CREATE TABLE IF NOT EXISTS %s (%s) %s' % \
                (self.jobsTableName, ','.join(fields), ','.join(options))

      cursor.execute(query)


    # ------------------------------------------------------------------------
    # Create the models table if it doesn't exist
    # Fields that start with '_eng' are intended for private use by the engine
    #  and should not be used by the UI
    if 'models' not in tableNames:
      self._logger.info("Creating table %r", self.modelsTableName)
      fields = [
        'model_id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT',
            # globally unique model ID
        'job_id                  INT UNSIGNED NOT NULL',
            # jobID
        'params                  LONGTEXT NOT NULL',
            # JSON encoded params for the model
        'status                  VARCHAR(16) DEFAULT "notStarted"',
            # One of the STATUS_XXX enumerated value strings
        'completion_reason       VARCHAR(16)',
            # One of the CMPL_REASON_XXX enumerated value strings
        'completion_msg          LONGTEXT',
            # Why this job completed
        'results                 LONGTEXT DEFAULT NULL',
            # JSON encoded structure containing metrics produced by the model
        'optimized_metric        FLOAT ',
            #Value of the particular metric we are optimizing in hypersearch
        'update_counter          INT UNSIGNED DEFAULT 0',
            # incremented by engine every time the results is updated
        'num_records             INT UNSIGNED DEFAULT 0',
            # number of records processed so far
        'start_time              DATETIME DEFAULT 0',
            # When this model started being evaluated
        'end_time                DATETIME DEFAULT 0',
            # When this model completed
        'cpu_time                FLOAT DEFAULT 0',
            # How much actual CPU time was spent on this model, in seconds. This
            #  excludes time the process spent sleeping, or otherwise not
            #  actually executing code.
        'model_checkpoint_id     LONGTEXT',
            # Checkpoint identifier for this model (after it has been saved)
        'gen_description         LONGTEXT',
            # The contents of the generated description.py file from hypersearch
            # requests. This is generated by the Hypersearch workers and stored
            # here for reference, debugging, and development purposes.
        '_eng_params_hash        BINARY(%d) DEFAULT NULL' % (self.HASH_MAX_LEN),
            # MD5 hash of the params
        '_eng_particle_hash      BINARY(%d) DEFAULT NULL' % (self.HASH_MAX_LEN),
            # MD5 hash of the particle info for PSO algorithm
        '_eng_last_update_time   DATETIME DEFAULT 0',
            # time stamp of last update, used for detecting stalled workers
        '_eng_task_tracker_id    TINYBLOB',
            # Hadoop Task Tracker ID
        '_eng_worker_id          TINYBLOB',
            # Hadoop Map Task ID
        '_eng_attempt_id         TINYBLOB',
            # Hadoop Map task attempt ID
        '_eng_worker_conn_id     INT DEFAULT 0',
            # database client connection ID of the worker that is running this
            # model
        '_eng_milestones         LONGTEXT',
            # A JSON encoded list of metric values for the model at each
            #  milestone point
        '_eng_stop               VARCHAR(16) DEFAULT NULL',
            # One of the STOP_REASON_XXX enumerated value strings. Set either by
            # the swarm terminator of either the current, or another
            # Hypersearch worker.
        '_eng_matured            BOOLEAN DEFAULT FALSE',
            # Set by the model maturity-checker when it decides that this model
            #  has "matured". This means that it has reached the point of
            #  not getting better results with more data.
        'PRIMARY KEY (model_id)',
        'UNIQUE INDEX (job_id, _eng_params_hash)',
        'UNIQUE INDEX (job_id, _eng_particle_hash)',
        ]
      options = [
        'AUTO_INCREMENT=1000',
        ]

      query = 'CREATE TABLE IF NOT EXISTS %s (%s) %s' % \
              (self.modelsTableName, ','.join(fields), ','.join(options))

      cursor.execute(query)


    # ---------------------------------------------------------------------
    # Get the field names for each table
    cursor.execute('DESCRIBE %s' % (self.jobsTableName))
    fields = cursor.fetchall()
    self._jobs.dbFieldNames = [str(field[0]) for field in fields]

    cursor.execute('DESCRIBE %s' % (self.modelsTableName))
    fields = cursor.fetchall()
    self._models.dbFieldNames = [str(field[0]) for field in fields]


    # ---------------------------------------------------------------------
    # Generate the public names
    self._jobs.publicFieldNames = [self._columnNameDBToPublic(x)
                                   for x in self._jobs.dbFieldNames]
    self._models.publicFieldNames = [self._columnNameDBToPublic(x)
                                     for x in self._models.dbFieldNames]


    # ---------------------------------------------------------------------
    # Generate the name conversion dicts
    self._jobs.pubToDBNameDict = dict(
      zip(self._jobs.publicFieldNames, self._jobs.dbFieldNames))
    self._jobs.dbToPubNameDict = dict(
      zip(self._jobs.dbFieldNames, self._jobs.publicFieldNames))
    self._models.pubToDBNameDict = dict(
      zip(self._models.publicFieldNames, self._models.dbFieldNames))
    self._models.dbToPubNameDict = dict(
      zip(self._models.dbFieldNames, self._models.publicFieldNames))


    # ---------------------------------------------------------------------
    # Generate the dynamic namedtuple classes we use
    self._models.modelInfoNamedTuple = collections.namedtuple(
      '_modelInfoNamedTuple', self._models.publicFieldNames)

    self._jobs.jobInfoNamedTuple = collections.namedtuple(
      '_jobInfoNamedTuple', self._jobs.publicFieldNames)

    return


  ##############################################################################
  def _getMatchingRowsNoRetries(self, tableInfo, conn, fieldsToMatch,
                                selectFieldNames, maxRows=None):
    """ Return a sequence of matching rows with the requested field values from
    a table or empty sequence if nothing matched.

    tableInfo:       Table information: a ClientJobsDAO._TableInfoBase  instance
    conn:            Owned connection acquired from ConnectionFactory.get()
    fieldsToMatch:   Dictionary of internal fieldName/value mappings that
                     identify the desired rows. If a value is an instance of
                     ClientJobsDAO._SEQUENCE_TYPES (list/set/tuple), then the
                     operator 'IN' will be used in the corresponding SQL
                     predicate; if the value is bool: "IS TRUE/FALSE"; if the
                     value is None: "IS NULL"; '=' will be used for all other
                     cases.
    selectFieldNames:
                     list of fields to return, using internal field names
    maxRows:         maximum number of rows to return; unlimited if maxRows
                      is None

    retval:          A sequence of matching rows, each row consisting of field
                      values in the order of the requested field names.  Empty
                      sequence is returned when not match exists.
    """

    assert fieldsToMatch, repr(fieldsToMatch)
    assert all(k in tableInfo.dbFieldNames
               for k in fieldsToMatch.iterkeys()), repr(fieldsToMatch)

    assert selectFieldNames, repr(selectFieldNames)
    assert all(f in tableInfo.dbFieldNames for f in selectFieldNames), repr(
      selectFieldNames)

    # NOTE: make sure match expressions and values are in the same order
    matchPairs = fieldsToMatch.items()
    matchExpressionGen = (
      p[0] +
      (' IS ' + {True:'TRUE', False:'FALSE'}[p[1]] if isinstance(p[1], bool)
       else ' IS NULL' if p[1] is None
       else ' IN %s' if isinstance(p[1], self._SEQUENCE_TYPES)
       else '=%s')
      for p in matchPairs)
    matchFieldValues = [p[1] for p in matchPairs
                        if (not isinstance(p[1], (bool)) and p[1] is not None)]

    query = 'SELECT %s FROM %s WHERE (%s)' % (
      ','.join(selectFieldNames), tableInfo.tableName,
      ' AND '.join(matchExpressionGen))
    sqlParams = matchFieldValues
    if maxRows is not None:
      query += ' LIMIT %s'
      sqlParams.append(maxRows)

    conn.cursor.execute(query, sqlParams)
    rows = conn.cursor.fetchall()

    if rows:
      assert maxRows is None or len(rows) <= maxRows, "%d !<= %d" % (
        len(rows), maxRows)
      assert len(rows[0]) == len(selectFieldNames), "%d != %d" % (
        len(rows[0]), len(selectFieldNames))
    else:
      rows = tuple()

    return rows


  ##############################################################################
  @g_retrySQL
  def _getMatchingRowsWithRetries(self, tableInfo, fieldsToMatch,
                                  selectFieldNames, maxRows=None):
    """ Like _getMatchingRowsNoRetries(), but with retries on transient MySQL
    failures
    """
    with ConnectionFactory.get() as conn:
      return self._getMatchingRowsNoRetries(tableInfo, conn, fieldsToMatch,
                                            selectFieldNames, maxRows)


  ##############################################################################
  def _getOneMatchingRowNoRetries(self, tableInfo, conn, fieldsToMatch,
                                  selectFieldNames):
    """ Return a single matching row with the requested field values from the
    the requested table or None if nothing matched.

    tableInfo:       Table information: a ClientJobsDAO._TableInfoBase  instance
    conn:            Owned connection acquired from ConnectionFactory.get()
    fieldsToMatch:   Dictionary of internal fieldName/value mappings that
                     identify the desired rows. If a value is an instance of
                     ClientJobsDAO._SEQUENCE_TYPES (list/set/tuple), then the
                     operator 'IN' will be used in the corresponding SQL
                     predicate; if the value is bool: "IS TRUE/FALSE"; if the
                     value is None: "IS NULL"; '=' will be used for all other
                     cases.
    selectFieldNames:
                     list of fields to return, using internal field names

    retval:          A sequence of field values of the matching row in the order
                      of the given field names; or None if there was no match.
    """
    rows = self._getMatchingRowsNoRetries(tableInfo, conn, fieldsToMatch,
                                          selectFieldNames, maxRows=1)
    if rows:
      assert len(rows) == 1, repr(len(rows))
      result = rows[0]
    else:
      result = None

    return result


  ##############################################################################
  @g_retrySQL
  def _getOneMatchingRowWithRetries(self, tableInfo, fieldsToMatch,
                                    selectFieldNames):
    """ Like _getOneMatchingRowNoRetries(), but with retries on transient MySQL
    failures
    """
    with ConnectionFactory.get() as conn:
      return self._getOneMatchingRowNoRetries(tableInfo, conn, fieldsToMatch,
                                              selectFieldNames)


  ##############################################################################
  @classmethod
  def _normalizeHash(cls, hashValue):
    hashLen = len(hashValue)
    if hashLen < cls.HASH_MAX_LEN:
      hashValue += '\0' * (cls.HASH_MAX_LEN - hashLen)
    else:
      assert hashLen <= cls.HASH_MAX_LEN, (
        "Hash is too long: hashLen=%r; hashValue=%r") % (hashLen, hashValue)

    return hashValue


  ##############################################################################
  def _insertOrGetUniqueJobNoRetries(
    self, conn, client, cmdLine, jobHash, clientInfo, clientKey, params,
    minimumWorkers, maximumWorkers, jobType, priority, alreadyRunning):
    """ Attempt to insert a row with the given parameters into the jobs table.
    Return jobID of the inserted row, or of an existing row with matching
    client/jobHash key.

    The combination of client and jobHash are expected to be unique (enforced
    by a unique index on the two columns).

    NOTE: It's possibe that this or another process (on this or another machine)
     already inserted a row with matching client/jobHash key (e.g.,
     StreamMgr). This may also happen undetected by this function due to a
     partially-successful insert operation (e.g., row inserted, but then
     connection was lost while reading response) followed by retries either of
     this function or in SteadyDB module.

    Parameters:
    ----------------------------------------------------------------
    conn:            Owned connection acquired from ConnectionFactory.get()
    client:          Name of the client submitting the job
    cmdLine:         Command line to use to launch each worker process; must be
                      a non-empty string
    jobHash:         unique hash of this job. The caller must insure that this,
                      together with client, uniquely identifies this job request
                      for the purposes of detecting duplicates.
    clientInfo:      JSON encoded dict of client specific information.
    clientKey:       Foreign key.
    params:          JSON encoded dict of the parameters for the job. This
                      can be fetched out of the database by the worker processes
                      based on the jobID.
    minimumWorkers:  minimum number of workers design at a time.
    maximumWorkers:  maximum number of workers desired at a time.
    priority:        Job scheduling priority; 0 is the default priority (
                      ClientJobsDAO.DEFAULT_JOB_PRIORITY); positive values are
                      higher priority (up to ClientJobsDAO.MAX_JOB_PRIORITY),
                      and negative values are lower priority (down to
                      ClientJobsDAO.MIN_JOB_PRIORITY). Higher-priority jobs will
                      be scheduled to run at the expense of the lower-priority
                      jobs, and higher-priority job tasks will preempt those
                      with lower priority if there is inadequate supply of
                      scheduling slots. Excess lower priority job tasks will
                      starve as long as slot demand exceeds supply. Most jobs
                      should be scheduled with DEFAULT_JOB_PRIORITY. System jobs
                      that must run at all cost, such as Multi-Model-Master,
                      should be scheduled with MAX_JOB_PRIORITY.
    alreadyRunning:  Used for unit test purposes only. This inserts the job
                      in the running state. It is used when running a worker
                      in standalone mode without hadoop- it gives it a job
                      record to work with.

    retval:           jobID of the inserted jobs row, or of an existing jobs row
                       with matching client/jobHash key
    """

    assert len(client) <= self.CLIENT_MAX_LEN, "client too long:" + repr(client)
    assert cmdLine, "Unexpected empty or None command-line: " + repr(cmdLine)
    assert len(jobHash) == self.HASH_MAX_LEN, "wrong hash len=%d" % len(jobHash)

    # Initial status
    if alreadyRunning:
      # STATUS_TESTMODE, so that scheduler won't pick it up (for in-proc tests)
      initStatus = self.STATUS_TESTMODE
    else:
      initStatus = self.STATUS_NOTSTARTED

    # Create a new job entry
    query = 'INSERT IGNORE INTO %s (status, client, client_info, client_key,' \
            'cmd_line, params, job_hash, _eng_last_update_time, ' \
            'minimum_workers, maximum_workers, priority, _eng_job_type) ' \
            ' VALUES (%%s, %%s, %%s, %%s, %%s, %%s, %%s, ' \
            '         UTC_TIMESTAMP(), %%s, %%s, %%s, %%s) ' \
            % (self.jobsTableName,)
    sqlParams = (initStatus, client, clientInfo, clientKey, cmdLine, params,
                 jobHash, minimumWorkers, maximumWorkers, priority, jobType)
    numRowsInserted = conn.cursor.execute(query, sqlParams)

    jobID = 0

    if numRowsInserted == 1:
      # Get the chosen job id
      # NOTE: LAST_INSERT_ID() returns 0 after intermittent connection failure
      conn.cursor.execute('SELECT LAST_INSERT_ID()')
      jobID = conn.cursor.fetchall()[0][0]
      if jobID == 0:
        self._logger.warn(
          '_insertOrGetUniqueJobNoRetries: SELECT LAST_INSERT_ID() returned 0; '
          'likely due to reconnection in SteadyDB following INSERT. '
          'jobType=%r; client=%r; clientInfo=%r; clientKey=%s; jobHash=%r; '
          'cmdLine=%r',
          jobType, client, _abbreviate(clientInfo, 32), clientKey, jobHash,
          cmdLine)
    else:
      # Assumption: nothing was inserted because this is a retry and the row
      # with this client/hash already exists from our prior
      # partially-successful attempt; or row with matching client/jobHash was
      # inserted already by some process on some machine.
      assert numRowsInserted == 0, repr(numRowsInserted)

    if jobID == 0:
      # Recover from intermittent failure in a partially-successful attempt;
      # or row with matching client/jobHash was already in table
      row = self._getOneMatchingRowNoRetries(
        self._jobs, conn, dict(client=client, job_hash=jobHash), ['job_id'])
      assert row is not None
      assert len(row) == 1, 'Unexpected num fields: ' + repr(len(row))
      jobID = row[0]

    # ---------------------------------------------------------------------
    # If asked to enter the job in the running state, set the connection id
    #  and start time as well
    if alreadyRunning:
      query = 'UPDATE %s SET _eng_cjm_conn_id=%%s, ' \
              '              start_time=UTC_TIMESTAMP(), ' \
              '              _eng_last_update_time=UTC_TIMESTAMP() ' \
              '          WHERE job_id=%%s' \
              % (self.jobsTableName,)
      conn.cursor.execute(query, (self._connectionID, jobID))

    return jobID


  ##############################################################################
  def _resumeJobNoRetries(self, conn, jobID, alreadyRunning):
    """ Resumes processing of an existing job that is presently in the
    STATUS_COMPLETED state.

    NOTE: this is primarily for resuming suspended Production and Stream Jobs; DO
     NOT use it on Hypersearch jobs.

    This prepares an existing job entry to resume processing. The CJM is always
    periodically sweeping the jobs table and when it finds a job that is ready
    to run, it will proceed to start it up on Hadoop.

    Parameters:
    ----------------------------------------------------------------
    conn:            Owned connection acquired from ConnectionFactory.get()
    jobID:          jobID of the job to resume
    alreadyRunning: Used for unit test purposes only. This inserts the job
                     in the running state. It is used when running a worker
                     in standalone mode without hadoop.

    raises:         Throws a RuntimeError if no rows are affected. This could
                    either be because:
                      1) Because there was not matching jobID
                      2) or if the status of the job was not STATUS_COMPLETED.

    retval:            nothing
    """

    # Initial status
    if alreadyRunning:
      # Use STATUS_TESTMODE so scheduler will leave our row alone
      initStatus = self.STATUS_TESTMODE
    else:
      initStatus = self.STATUS_NOTSTARTED

    # NOTE: some of our clients (e.g., StreamMgr) may call us (directly or
    #  indirectly) for the same job from different processes (even different
    #  machines), so we should be prepared for the update to fail; same holds
    #  if the UPDATE succeeds, but connection fails while reading result
    assignments = [
      'status=%s',
      'completion_reason=DEFAULT',
      'completion_msg=DEFAULT',
      'worker_completion_reason=DEFAULT',
      'worker_completion_msg=DEFAULT',
      'end_time=DEFAULT',
      'cancel=DEFAULT',
      '_eng_last_update_time=UTC_TIMESTAMP()',
      '_eng_allocate_new_workers=DEFAULT',
      '_eng_untended_dead_workers=DEFAULT',
      'num_failed_workers=DEFAULT',
      'last_failed_worker_error_msg=DEFAULT',
      '_eng_cleaning_status=DEFAULT',
    ]
    assignmentValues = [initStatus]

    if alreadyRunning:
      assignments += ['_eng_cjm_conn_id=%s', 'start_time=UTC_TIMESTAMP()',
                      '_eng_last_update_time=UTC_TIMESTAMP()']
      assignmentValues.append(self._connectionID)
    else:
      assignments += ['_eng_cjm_conn_id=DEFAULT', 'start_time=DEFAULT']

    assignments = ', '.join(assignments)

    query = 'UPDATE %s SET %s ' \
              '          WHERE job_id=%%s AND status=%%s' \
              % (self.jobsTableName, assignments)
    sqlParams = assignmentValues + [jobID, self.STATUS_COMPLETED]

    numRowsAffected = conn.cursor.execute(query, sqlParams)

    assert numRowsAffected <= 1, repr(numRowsAffected)

    if numRowsAffected == 0:
      self._logger.info(
        "_resumeJobNoRetries: Redundant job-resume UPDATE: job was not "
        "suspended or was resumed by another process or operation was retried "
        "after connection failure; jobID=%s", jobID)

    return


  ############################################################################
  def getConnectionID(self):
    """ Return our connection ID. This can be used for worker identification
    purposes.

    NOTE: the actual MySQL connection ID used in queries may change from time
     to time if connection is re-acquired (e.g., upon MySQL server restart) or
     when more than one entry from the connection pool has been used (e.g.,
     multi-threaded apps)
    """

    return self._connectionID


  ##############################################################################
  @logExceptions(_getLogger)
  def jobSuspend(self, jobID):
    """ Requests a job to be suspended

    NOTE: this is primarily for suspending Production Jobs; DO NOT use
    it on Hypersearch jobs. For canceling any job type, use jobCancel() instead!

    Parameters:
    ----------------------------------------------------------------
    jobID:          jobID of the job to resume

    retval:            nothing
    """

    # TODO: validate that the job is in the appropriate state for being
    #       suspended: consider using a WHERE clause to make sure that
    #       the job is not already in the "completed" state

    # TODO: when Nupic job control states get figured out, there may be a
    #       different way to suspend jobs ("cancel" doesn't make sense for this)

    # NOTE: jobCancel() does retries on transient mysql failures
    self.jobCancel(jobID)

    return


  ##############################################################################
  @logExceptions(_getLogger)
  def jobResume(self, jobID, alreadyRunning=False):
    """ Resumes processing of an existing job that is presently in the
    STATUS_COMPLETED state.

    NOTE: this is primarily for resuming suspended Production Jobs; DO NOT use
    it on Hypersearch jobs.

    NOTE: The job MUST be in the STATUS_COMPLETED state at the time of this
    call, otherwise an exception will be raised.

    This prepares an existing job entry to resume processing. The CJM is always
    periodically sweeping the jobs table and when it finds a job that is ready
    to run, will proceed to start it up on Hadoop.

    Parameters:
    ----------------------------------------------------------------
    job:            jobID of the job to resume
    alreadyRunning: Used for unit test purposes only. This inserts the job
                     in the running state. It is used when running a worker
                     in standalone mode without hadoop.

    raises:         Throws a RuntimeError if no rows are affected. This could
                    either be because:
                      1) Because there was not matching jobID
                      2) or if the status of the job was not STATUS_COMPLETED.

    retval:            nothing
    """

    row = self.jobGetFields(jobID, ['status'])
    (jobStatus,) = row
    if jobStatus != self.STATUS_COMPLETED:
      raise RuntimeError(("Failed to resume job: job was not suspended; "
                          "jobID=%s; job status=%r") % (jobID, jobStatus))

    # NOTE: on MySQL failures, we need to retry ConnectionFactory.get() as well
    #  in order to recover from lost connections
    @g_retrySQL
    def resumeWithRetries():
      with ConnectionFactory.get() as conn:
        self._resumeJobNoRetries(conn, jobID, alreadyRunning)

    resumeWithRetries()
    return


  ##############################################################################
  @logExceptions(_getLogger)
  def jobInsert(self, client, cmdLine, clientInfo='', clientKey='', params='',
                alreadyRunning=False, minimumWorkers=0, maximumWorkers=0,
                jobType='', priority=DEFAULT_JOB_PRIORITY):
    """ Add an entry to the jobs table for a new job request. This is called by
    clients that wish to startup a new job, like a Hypersearch, stream job, or
    specific model evaluation from the engine.

    This puts a new entry into the jobs table. The CJM is always periodically
    sweeping the jobs table and when it finds a new job, will proceed to start it
    up on Hadoop.

    Parameters:
    ----------------------------------------------------------------
    client:          Name of the client submitting the job
    cmdLine:         Command line to use to launch each worker process; must be
                      a non-empty string
    clientInfo:      JSON encoded dict of client specific information.
    clientKey:       Foreign key.
    params:          JSON encoded dict of the parameters for the job. This
                      can be fetched out of the database by the worker processes
                      based on the jobID.
    alreadyRunning:  Used for unit test purposes only. This inserts the job
                      in the running state. It is used when running a worker
                      in standalone mode without hadoop - it gives it a job
                      record to work with.
    minimumWorkers:  minimum number of workers design at a time.
    maximumWorkers:  maximum number of workers desired at a time.
    jobType:         The type of job that this is. This should be one of the
                      JOB_TYPE_XXXX enums. This is needed to allow a standard
                      way of recognizing a job's function and capabilities.
    priority:        Job scheduling priority; 0 is the default priority (
                      ClientJobsDAO.DEFAULT_JOB_PRIORITY); positive values are
                      higher priority (up to ClientJobsDAO.MAX_JOB_PRIORITY),
                      and negative values are lower priority (down to
                      ClientJobsDAO.MIN_JOB_PRIORITY). Higher-priority jobs will
                      be scheduled to run at the expense of the lower-priority
                      jobs, and higher-priority job tasks will preempt those
                      with lower priority if there is inadequate supply of
                      scheduling slots. Excess lower priority job tasks will
                      starve as long as slot demand exceeds supply. Most jobs
                      should be scheduled with DEFAULT_JOB_PRIORITY. System jobs
                      that must run at all cost, such as Multi-Model-Master,
                      should be scheduled with MAX_JOB_PRIORITY.

    retval:          jobID - unique ID assigned to this job
    """

    jobHash = self._normalizeHash(uuid.uuid1().bytes)

    @g_retrySQL
    def insertWithRetries():
      with ConnectionFactory.get() as conn:
        return self._insertOrGetUniqueJobNoRetries(
          conn, client=client, cmdLine=cmdLine, jobHash=jobHash,
          clientInfo=clientInfo, clientKey=clientKey, params=params,
          minimumWorkers=minimumWorkers, maximumWorkers=maximumWorkers,
          jobType=jobType, priority=priority, alreadyRunning=alreadyRunning)

    try:
      jobID = insertWithRetries()
    except:
      self._logger.exception(
        'jobInsert FAILED: jobType=%r; client=%r; clientInfo=%r; clientKey=%r;'
        'jobHash=%r; cmdLine=%r',
        jobType, client, _abbreviate(clientInfo, 48), clientKey, jobHash,
        cmdLine)
      raise
    else:
      self._logger.info(
        'jobInsert: returning jobID=%s. jobType=%r; client=%r; clientInfo=%r; '
        'clientKey=%r; jobHash=%r; cmdLine=%r',
        jobID, jobType, client, _abbreviate(clientInfo, 48), clientKey,
        jobHash, cmdLine)

    return jobID


  ##############################################################################
  @logExceptions(_getLogger)
  def jobInsertUnique(self, client, cmdLine, jobHash, clientInfo='',
                      clientKey='', params='', minimumWorkers=0,
                      maximumWorkers=0, jobType='',
                      priority=DEFAULT_JOB_PRIORITY):
    """ Add an entry to the jobs table for a new job request, but only if the
    same job, by the same client is not already running. If the job is already
    running, or queued up to run, this call does nothing. If the job does not
    exist in the jobs table or has completed, it will be inserted and/or started
    up again.

    This method is called by clients, like StreamMgr, that wish to only start up
    a job if it hasn't already been started up.

    Parameters:
    ----------------------------------------------------------------
    client:          Name of the client submitting the job
    cmdLine:         Command line to use to launch each worker process; must be
                      a non-empty string
    jobHash:         unique hash of this job. The client must insure that this
                      uniquely identifies this job request for the purposes
                      of detecting duplicates.
    clientInfo:      JSON encoded dict of client specific information.
    clientKey:       Foreign key.
    params:          JSON encoded dict of the parameters for the job. This
                      can be fetched out of the database by the worker processes
                      based on the jobID.
    minimumWorkers:  minimum number of workers design at a time.
    maximumWorkers:  maximum number of workers desired at a time.
    jobType:         The type of job that this is. This should be one of the
                      JOB_TYPE_XXXX enums. This is needed to allow a standard
                      way of recognizing a job's function and capabilities.
    priority:        Job scheduling priority; 0 is the default priority (
                      ClientJobsDAO.DEFAULT_JOB_PRIORITY); positive values are
                      higher priority (up to ClientJobsDAO.MAX_JOB_PRIORITY),
                      and negative values are lower priority (down to
                      ClientJobsDAO.MIN_JOB_PRIORITY). Higher-priority jobs will
                      be scheduled to run at the expense of the lower-priority
                      jobs, and higher-priority job tasks will preempt those
                      with lower priority if there is inadequate supply of
                      scheduling slots. Excess lower priority job tasks will
                      starve as long as slot demand exceeds supply. Most jobs
                      should be scheduled with DEFAULT_JOB_PRIORITY. System jobs
                      that must run at all cost, such as Multi-Model-Master,
                      should be scheduled with MAX_JOB_PRIORITY.

    retval:          jobID of the newly inserted or existing job.
    """

    assert cmdLine, "Unexpected empty or None command-line: " + repr(cmdLine)

    @g_retrySQL
    def insertUniqueWithRetries():
      jobHashValue = self._normalizeHash(jobHash)

      jobID = None
      with ConnectionFactory.get() as conn:
        row = self._getOneMatchingRowNoRetries(
          self._jobs, conn, dict(client=client, job_hash=jobHashValue),
          ['job_id', 'status'])

        if row is not None:
          (jobID, status) = row

          if status == self.STATUS_COMPLETED:
            # Restart existing job that had completed
            query = 'UPDATE %s SET client_info=%%s, ' \
                    '              client_key=%%s, ' \
                    '              cmd_line=%%s, ' \
                    '              params=%%s, ' \
                    '              minimum_workers=%%s, ' \
                    '              maximum_workers=%%s, ' \
                    '              priority=%%s, '\
                    '              _eng_job_type=%%s ' \
                    '          WHERE (job_id=%%s AND status=%%s)' \
                    % (self.jobsTableName,)
            sqlParams = (clientInfo, clientKey, cmdLine, params,
                         minimumWorkers, maximumWorkers, priority,
                         jobType, jobID, self.STATUS_COMPLETED)

            numRowsUpdated = conn.cursor.execute(query, sqlParams)
            assert numRowsUpdated <= 1, repr(numRowsUpdated)

            if numRowsUpdated == 0:
              self._logger.info(
                "jobInsertUnique: Redundant job-reuse UPDATE: job restarted by "
                "another process, values were unchanged, or operation was "
                "retried after connection failure; jobID=%s", jobID)

            # Restart the job, unless another process beats us to it
            self._resumeJobNoRetries(conn, jobID, alreadyRunning=False)
        else:
          # There was no job row with matching client/jobHash, so insert one
          jobID = self._insertOrGetUniqueJobNoRetries(
            conn, client=client, cmdLine=cmdLine, jobHash=jobHashValue,
            clientInfo=clientInfo, clientKey=clientKey, params=params,
            minimumWorkers=minimumWorkers, maximumWorkers=maximumWorkers,
            jobType=jobType, priority=priority, alreadyRunning=False)

        return jobID

    try:
      jobID = insertUniqueWithRetries()
    except:
      self._logger.exception(
        'jobInsertUnique FAILED: jobType=%r; client=%r; '
        'clientInfo=%r; clientKey=%r; jobHash=%r; cmdLine=%r',
        jobType, client, _abbreviate(clientInfo, 48), clientKey, jobHash,
        cmdLine)
      raise
    else:
      self._logger.info(
        'jobInsertUnique: returning jobID=%s. jobType=%r; client=%r; '
        'clientInfo=%r; clientKey=%r; jobHash=%r; cmdLine=%r',
        jobID, jobType, client, _abbreviate(clientInfo, 48), clientKey,
        jobHash, cmdLine)

    return jobID


  ##############################################################################
  @g_retrySQL
  def _startJobWithRetries(self, jobID):
    """ Place the given job in STATUS_RUNNING mode; the job is expected to be
    STATUS_NOTSTARTED.

    NOTE: this function was factored out of jobStartNext because it's also
     needed for testing (e.g., test_client_jobs_dao.py)
    """
    with ConnectionFactory.get() as conn:
      query = 'UPDATE %s SET status=%%s, ' \
                '            _eng_cjm_conn_id=%%s, ' \
                '            start_time=UTC_TIMESTAMP(), ' \
                '            _eng_last_update_time=UTC_TIMESTAMP() ' \
                '          WHERE (job_id=%%s AND status=%%s)' \
                % (self.jobsTableName,)
      sqlParams = [self.STATUS_RUNNING, self._connectionID,
                   jobID, self.STATUS_NOTSTARTED]
      numRowsUpdated = conn.cursor.execute(query, sqlParams)
      if numRowsUpdated != 1:
        self._logger.warn('jobStartNext: numRowsUpdated=%r instead of 1; '
                          'likely side-effect of transient connection '
                          'failure', numRowsUpdated)
    return


  ##############################################################################
  @logExceptions(_getLogger)
  def jobStartNext(self):
    """ For use only by Nupic Scheduler (also known as ClientJobManager) Look
    through the jobs table and see if any new job requests have been
    queued up. If so, pick one and mark it as starting up and create the
    model table to hold the results

    Parameters:
    ----------------------------------------------------------------
    retval:    jobID of the job we are starting up, if found; None if not found
    """

    # NOTE: cursor.execute('SELECT @update_id') trick is unreliable: if a
    #  connection loss occurs during cursor.execute, then the server-cached
    #  information is lost, and we cannot get the updated job ID; so, we use
    #  this select instead
    row = self._getOneMatchingRowWithRetries(
      self._jobs, dict(status=self.STATUS_NOTSTARTED), ['job_id'])
    if row is None:
      return None

    (jobID,) = row

    self._startJobWithRetries(jobID)

    return jobID


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def jobReactivateRunningJobs(self):
    """ Look through the jobs table and reactivate all that are already in the
    running state by setting their _eng_allocate_new_workers fields to True;
    used by Nupic Scheduler as part of its failure-recovery procedure.
    """

    # Get a database connection and cursor
    with ConnectionFactory.get() as conn:

      query = 'UPDATE %s SET _eng_cjm_conn_id=%%s, ' \
              '              _eng_allocate_new_workers=TRUE ' \
              '    WHERE status=%%s ' \
              % (self.jobsTableName,)
      conn.cursor.execute(query, [self._connectionID, self.STATUS_RUNNING])

    return


  ##############################################################################
  @logExceptions(_getLogger)
  def jobGetDemand(self,):
    """ Look through the jobs table and get the demand - minimum and maximum
    number of workers requested, if new workers are to be allocated, if there
    are any untended dead workers, for all running jobs.

    Parameters:
    ----------------------------------------------------------------
    retval:      list of ClientJobsDAO._jobs.jobDemandNamedTuple nametuples
                  containing the demand - min and max workers,
                  allocate_new_workers, untended_dead_workers, num_failed_workers
                  for each running (STATUS_RUNNING) job. Empty list when there
                  isn't any demand.

    """
    rows = self._getMatchingRowsWithRetries(
      self._jobs, dict(status=self.STATUS_RUNNING),
      [self._jobs.pubToDBNameDict[f]
       for f in self._jobs.jobDemandNamedTuple._fields])

    return [self._jobs.jobDemandNamedTuple._make(r) for r in rows]


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def jobCancelAllRunningJobs(self):
    """ Set cancel field of all currently-running jobs to true.
    """

    # Get a database connection and cursor
    with ConnectionFactory.get() as conn:

      query = 'UPDATE %s SET cancel=TRUE WHERE status<>%%s ' \
              % (self.jobsTableName,)
      conn.cursor.execute(query, [self.STATUS_COMPLETED])

    return


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def jobCountCancellingJobs(self,):
    """ Look through the jobs table and count the running jobs whose
    cancel field is true.

    Parameters:
    ----------------------------------------------------------------
    retval:      A count of running jobs with the cancel field set to true.
    """
    with ConnectionFactory.get() as conn:
      query = 'SELECT COUNT(job_id) '\
              'FROM %s ' \
              'WHERE (status<>%%s AND cancel is TRUE)' \
              % (self.jobsTableName,)

      conn.cursor.execute(query, [self.STATUS_COMPLETED])
      rows = conn.cursor.fetchall()

    return rows[0][0]


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def jobGetCancellingJobs(self,):
    """ Look through the jobs table and get the list of running jobs whose
    cancel field is true.

    Parameters:
    ----------------------------------------------------------------
    retval:      A (possibly empty) sequence of running job IDs with cancel field
                  set to true
    """
    with ConnectionFactory.get() as conn:
      query = 'SELECT job_id '\
              'FROM %s ' \
              'WHERE (status<>%%s AND cancel is TRUE)' \
              % (self.jobsTableName,)
      conn.cursor.execute(query, [self.STATUS_COMPLETED])
      rows = conn.cursor.fetchall()

    return tuple(r[0] for r in rows)


  ##############################################################################
  @staticmethod
  @logExceptions(_getLogger)
  def partitionAtIntervals(data, intervals):
    """ Generator to allow iterating slices at dynamic intervals

    Parameters:
    ----------------------------------------------------------------
    data:       Any data structure that supports slicing (i.e. list or tuple)
    *intervals: Iterable of intervals.  The sum of intervals should be less
                than, or equal to the length of data.

    """
    assert sum(intervals) <= len(data)

    start = 0
    for interval in intervals:
      end = start + interval
      yield data[start:end]
      start = end

    raise StopIteration

  ##############################################################################
  @staticmethod
  @logExceptions(_getLogger)
  def _combineResults(result, *namedTuples):
    """ Return a list of namedtuples from the result of a join query.  A
    single database result is partitioned at intervals corresponding to the
    fields in namedTuples.  The return value is the result of applying
    namedtuple._make() to each of the partitions, for each of the namedTuples.

    Parameters:
    ----------------------------------------------------------------
    result:         Tuple representing a single result from a database query
    *namedTuples:   List of named tuples.

    """
    results = ClientJobsDAO.partitionAtIntervals(
      result, [len(nt._fields) for nt in namedTuples])
    return [nt._make(result) for nt, result in zip(namedTuples, results)]

  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def jobInfoWithModels(self, jobID):
    """ Get all info about a job, with model details, if available.

    Parameters:
    ----------------------------------------------------------------
    job:    jobID of the job to query
    retval: A sequence of two-tuples if the jobID exists in the jobs
             table (exeption is raised if it doesn't exist). Each two-tuple
             contains an instance of jobInfoNamedTuple as the first element and
             an instance of modelInfoNamedTuple as the second element. NOTE: In
             the case where there are no matching model rows, a sequence of one
             two-tuple will still be returned, but the modelInfoNamedTuple
             fields will be None, and the jobInfoNamedTuple fields will be
             populated.
    """

    # Get a database connection and cursor
    combinedResults = None

    with ConnectionFactory.get() as conn:
      # NOTE: Since we're using a LEFT JOIN on the models table, there need not
      # be a matching row in the models table, but the matching row from the
      # jobs table will still be returned (along with all fields from the models
      # table with values of None in case there were no matchings models)
      query = ' '.join([
        'SELECT %s.*, %s.*' % (self.jobsTableName, self.modelsTableName),
        'FROM %s' % self.jobsTableName,
        'LEFT JOIN %s USING(job_id)' % self.modelsTableName,
        'WHERE job_id=%s'])

      conn.cursor.execute(query, (jobID,))

      if conn.cursor.rowcount > 0:
        combinedResults = [
          ClientJobsDAO._combineResults(
            result, self._jobs.jobInfoNamedTuple,
            self._models.modelInfoNamedTuple
          ) for result in conn.cursor.fetchall()]

    if combinedResults is not None:
      return combinedResults

    raise RuntimeError("jobID=%s not found within the jobs table" % (jobID))


  ##############################################################################
  @logExceptions(_getLogger)
  def jobInfo(self, jobID):
    """ Get all info about a job

    Parameters:
    ----------------------------------------------------------------
    job:    jobID of the job to query
    retval:  namedtuple containing the job info.

    """
    row = self._getOneMatchingRowWithRetries(
      self._jobs, dict(job_id=jobID),
      [self._jobs.pubToDBNameDict[n]
       for n in self._jobs.jobInfoNamedTuple._fields])

    if row is None:
      raise RuntimeError("jobID=%s not found within the jobs table" % (jobID))

    # Create a namedtuple with the names to values
    return self._jobs.jobInfoNamedTuple._make(row)


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def jobSetStatus(self, jobID, status, useConnectionID=True,):
    """ Change the status on the given job

    Parameters:
    ----------------------------------------------------------------
    job:        jobID of the job to change status
    status:     new status string (ClientJobsDAO.STATUS_xxxxx)

    useConnectionID: True if the connection id of the calling function
    must be the same as the connection that created the job. Set
    to False for hypersearch workers
    """
    # Get a database connection and cursor
    with ConnectionFactory.get() as conn:
      query = 'UPDATE %s SET status=%%s, ' \
              '              _eng_last_update_time=UTC_TIMESTAMP() ' \
              '          WHERE job_id=%%s' \
              % (self.jobsTableName,)
      sqlParams = [status, jobID]

      if useConnectionID:
        query += ' AND _eng_cjm_conn_id=%s'
        sqlParams.append(self._connectionID)

      result = conn.cursor.execute(query, sqlParams)

      if result != 1:
        raise RuntimeError("Tried to change the status of job %d to %s, but "
                           "this job belongs to some other CJM" % (
                            jobID, status))


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def jobSetCompleted(self, jobID, completionReason, completionMsg,
                      useConnectionID = True):
    """ Change the status on the given job to completed

    Parameters:
    ----------------------------------------------------------------
    job:                 jobID of the job to mark as completed
    completionReason:    completionReason string
    completionMsg:       completionMsg string

    useConnectionID: True if the connection id of the calling function
    must be the same as the connection that created the job. Set
    to False for hypersearch workers
    """

    # Get a database connection and cursor
    with ConnectionFactory.get() as conn:
      query = 'UPDATE %s SET status=%%s, ' \
              '              completion_reason=%%s, ' \
              '              completion_msg=%%s, ' \
              '              end_time=UTC_TIMESTAMP(), ' \
              '              _eng_last_update_time=UTC_TIMESTAMP() ' \
              '          WHERE job_id=%%s' \
              % (self.jobsTableName,)
      sqlParams = [self.STATUS_COMPLETED, completionReason, completionMsg,
                   jobID]

      if useConnectionID:
        query += ' AND _eng_cjm_conn_id=%s'
        sqlParams.append(self._connectionID)

      result = conn.cursor.execute(query, sqlParams)

      if result != 1:
        raise RuntimeError("Tried to change the status of jobID=%s to "
                           "completed, but this job could not be found or "
                           "belongs to some other CJM" % (jobID))


  ##############################################################################
  @logExceptions(_getLogger)
  def jobCancel(self, jobID):
    """ Cancel the given job. This will update the cancel field in the
    jobs table and will result in the job being cancelled.

    Parameters:
    ----------------------------------------------------------------
    jobID:                 jobID of the job to mark as completed

    to False for hypersearch workers
    """
    self._logger.info('Canceling jobID=%s', jobID)
    # NOTE: jobSetFields does retries on transient mysql failures
    self.jobSetFields(jobID, {"cancel" : True}, useConnectionID=False)


  ##############################################################################
  @logExceptions(_getLogger)
  def jobGetModelIDs(self, jobID):
    """Fetch all the modelIDs that correspond to a given jobID; empty sequence
    if none"""

    rows = self._getMatchingRowsWithRetries(self._models, dict(job_id=jobID),
                                            ['model_id'])
    return [r[0] for r in rows]


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def getActiveJobCountForClientInfo(self, clientInfo):
    """ Return the number of jobs for the given clientInfo and a status that is
    not completed.
    """
    with ConnectionFactory.get() as conn:
      query = 'SELECT count(job_id) ' \
              'FROM %s ' \
              'WHERE client_info = %%s ' \
              ' AND status != %%s' %  self.jobsTableName
      conn.cursor.execute(query, [clientInfo, self.STATUS_COMPLETED])
      activeJobCount = conn.cursor.fetchone()[0]

    return activeJobCount


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def getActiveJobCountForClientKey(self, clientKey):
    """ Return the number of jobs for the given clientKey and a status that is
    not completed.
    """
    with ConnectionFactory.get() as conn:
      query = 'SELECT count(job_id) ' \
              'FROM %s ' \
              'WHERE client_key = %%s ' \
              ' AND status != %%s' %  self.jobsTableName
      conn.cursor.execute(query, [clientKey, self.STATUS_COMPLETED])
      activeJobCount = conn.cursor.fetchone()[0]

    return activeJobCount


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def getActiveJobsForClientInfo(self, clientInfo, fields=[]):
    """ Fetch jobIDs for jobs in the table with optional fields given a
    specific clientInfo """

    # Form the sequence of field name strings that will go into the
    #  request
    dbFields = [self._jobs.pubToDBNameDict[x] for x in fields]
    dbFieldsStr = ','.join(['job_id'] + dbFields)

    with ConnectionFactory.get() as conn:
      query = 'SELECT %s FROM %s ' \
              'WHERE client_info = %%s ' \
              ' AND status != %%s' % (dbFieldsStr, self.jobsTableName)
      conn.cursor.execute(query, [clientInfo, self.STATUS_COMPLETED])
      rows = conn.cursor.fetchall()

    return rows


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def getActiveJobsForClientKey(self, clientKey, fields=[]):
    """ Fetch jobIDs for jobs in the table with optional fields given a
    specific clientKey """

    # Form the sequence of field name strings that will go into the
    #  request
    dbFields = [self._jobs.pubToDBNameDict[x] for x in fields]
    dbFieldsStr = ','.join(['job_id'] + dbFields)

    with ConnectionFactory.get() as conn:
      query = 'SELECT %s FROM %s ' \
              'WHERE client_key = %%s ' \
              ' AND status != %%s' % (dbFieldsStr, self.jobsTableName)
      conn.cursor.execute(query, [clientKey, self.STATUS_COMPLETED])
      rows = conn.cursor.fetchall()

    return rows


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def getJobs(self, fields=[]):
    """ Fetch jobIDs for jobs in the table with optional fields """

    # Form the sequence of field name strings that will go into the
    #  request
    dbFields = [self._jobs.pubToDBNameDict[x] for x in fields]
    dbFieldsStr = ','.join(['job_id'] + dbFields)

    with ConnectionFactory.get() as conn:
      query = 'SELECT %s FROM %s' % (dbFieldsStr, self.jobsTableName)
      conn.cursor.execute(query)
      rows = conn.cursor.fetchall()

    return rows


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def getFieldsForActiveJobsOfType(self, jobType, fields=[]):
    """ Helper function for querying the models table including relevant job
    info where the job type matches the specified jobType.  Only records for
    which there is a matching jobId in both tables is returned, and only the
    requested fields are returned in each result, assuming that there is not
    a conflict.  This function is useful, for example, in querying a cluster
    for a list of actively running production models (according to the state
    of the client jobs database).  jobType must be one of the JOB_TYPE_XXXX
    enumerations.

    Parameters:
    ----------------------------------------------------------------
    jobType:   jobType enum
    fields:    list of fields to return

    Returns:    List of tuples containing the jobId and requested field values
    """
    dbFields = [self._jobs.pubToDBNameDict[x] for x in fields]
    dbFieldsStr = ','.join(['job_id'] + dbFields)
    with ConnectionFactory.get() as conn:
      query = \
        'SELECT DISTINCT %s ' \
        'FROM %s j ' \
        'LEFT JOIN %s m USING(job_id) '\
        'WHERE j.status != %%s ' \
          'AND _eng_job_type = %%s' % (dbFieldsStr, self.jobsTableName,
            self.modelsTableName)

      conn.cursor.execute(query, [self.STATUS_COMPLETED, jobType])
      return conn.cursor.fetchall()


  ##############################################################################
  @logExceptions(_getLogger)
  def jobGetFields(self, jobID, fields):
    """ Fetch the values of 1 or more fields from a job record. Here, 'fields'
    is a list with the names of the fields to fetch. The names are the public
    names of the fields (camelBack, not the lower_case_only form as stored in
    the DB).

    Parameters:
    ----------------------------------------------------------------
    jobID:     jobID of the job record
    fields:    list of fields to return

    Returns:    A sequence of field values in the same order as the requested
                 field list -> [field1, field2, ...]
    """
    # NOTE: jobsGetFields retries on transient mysql failures
    return self.jobsGetFields([jobID], fields, requireAll=True)[0][1]


  ##############################################################################
  @logExceptions(_getLogger)
  def jobsGetFields(self, jobIDs, fields, requireAll=True):
    """ Fetch the values of 1 or more fields from a sequence of job records.
    Here, 'fields' is a sequence (list or tuple) with the names of the fields to
    fetch. The names are the public names of the fields (camelBack, not the
    lower_case_only form as stored in the DB).

    WARNING!!!: The order of the results are NOT necessarily in the same order as
    the order of the job IDs passed in!!!

    Parameters:
    ----------------------------------------------------------------
    jobIDs:        A sequence of jobIDs
    fields:        A list  of fields to return for each jobID

    Returns:      A list of tuples->(jobID, [field1, field2,...])
    """
    assert isinstance(jobIDs, self._SEQUENCE_TYPES)
    assert len(jobIDs) >=1

    rows = self._getMatchingRowsWithRetries(
      self._jobs, dict(job_id=jobIDs),
      ['job_id'] + [self._jobs.pubToDBNameDict[x] for x in fields])


    if requireAll and len(rows) < len(jobIDs):
      # NOTE: this will also trigger if the jobIDs list included duplicates
      raise RuntimeError("jobIDs %s not found within the jobs table" % (
        (set(jobIDs) - set(r[0] for r in rows)),))


    return [(r[0], list(r[1:])) for r in rows]


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def jobSetFields(self, jobID, fields, useConnectionID=True,
                   ignoreUnchanged=False):
    """ Change the values of 1 or more fields in a job. Here, 'fields' is a
    dict with the name/value pairs to change. The names are the public names of
    the fields (camelBack, not the lower_case_only form as stored in the DB).
    This method is for private use by the ClientJobManager only.

    Parameters:
    ----------------------------------------------------------------
    jobID:     jobID of the job record

    fields:    dictionary of fields to change

    useConnectionID: True if the connection id of the calling function
    must be the same as the connection that created the job. Set
    to False for hypersearch workers

    ignoreUnchanged: The default behavior is to throw a
    RuntimeError if no rows are affected. This could either be
    because:
      1) Because there was not matching jobID
      2) or if the data to update matched the data in the DB exactly.

    Set this parameter to True if you expect case 2 and wish to
    supress the error.
    """

    # Form the sequecce of key=value strings that will go into the
    #  request
    assignmentExpressions = ','.join(
      ["%s=%%s" % (self._jobs.pubToDBNameDict[f],) for f in fields.iterkeys()])
    assignmentValues = fields.values()

    query = 'UPDATE %s SET %s ' \
            '          WHERE job_id=%%s' \
            % (self.jobsTableName, assignmentExpressions,)
    sqlParams = assignmentValues + [jobID]

    if useConnectionID:
      query += ' AND _eng_cjm_conn_id=%s'
      sqlParams.append(self._connectionID)

    # Get a database connection and cursor
    with ConnectionFactory.get() as conn:
      result = conn.cursor.execute(query, sqlParams)

    if result != 1 and not ignoreUnchanged:
      raise RuntimeError(
        "Tried to change fields (%r) of jobID=%s conn_id=%r), but an error " \
        "occurred. result=%r; query=%r" % (
          assignmentExpressions, jobID, self._connectionID, result, query))


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def jobSetFieldIfEqual(self, jobID, fieldName, newValue, curValue):
    """ Change the value of 1 field in a job to 'newValue', but only if the
    current value matches 'curValue'. The 'fieldName' is the public name of
    the field (camelBack, not the lower_case_only form as stored in the DB).

    This method is used for example by HypersearcWorkers to update the
    engWorkerState field periodically. By qualifying on curValue, it insures
    that only 1 worker at a time is elected to perform the next scheduled
    periodic sweep of the models.

    Parameters:
    ----------------------------------------------------------------
    jobID:        jobID of the job record to modify
    fieldName:    public field name of the field
    newValue:     new value of the field to set
    curValue:     current value to qualify against

    retval:       True if we successfully modified the field
                  False if curValue did not match
    """

    # Get the private field name and string form of the value
    dbFieldName = self._jobs.pubToDBNameDict[fieldName]

    conditionValue = []
    if isinstance(curValue, bool):
      conditionExpression = '%s IS %s' % (
        dbFieldName, {True:'TRUE', False:'FALSE'}[curValue])
    elif curValue is None:
      conditionExpression = '%s is NULL' % (dbFieldName,)
    else:
      conditionExpression = '%s=%%s' % (dbFieldName,)
      conditionValue.append(curValue)

    query = 'UPDATE %s SET _eng_last_update_time=UTC_TIMESTAMP(), %s=%%s ' \
            '          WHERE job_id=%%s AND %s' \
            % (self.jobsTableName, dbFieldName, conditionExpression)
    sqlParams = [newValue, jobID] + conditionValue

    with ConnectionFactory.get() as conn:
      result = conn.cursor.execute(query, sqlParams)

    return (result == 1)


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def jobIncrementIntField(self, jobID, fieldName, increment=1,
                           useConnectionID=False):
    """ Incremet the value of 1 field in a job by increment. The 'fieldName' is
    the public name of the field (camelBack, not the lower_case_only form as
    stored in the DB).

    This method is used for example by HypersearcWorkers to update the
    engWorkerState field periodically. By qualifying on curValue, it insures
    that only 1 worker at a time is elected to perform the next scheduled
    periodic sweep of the models.

    Parameters:
    ----------------------------------------------------------------
    jobID:        jobID of the job record to modify
    fieldName:    public field name of the field
    increment:    increment is added to the current value of the field
    """
    # Get the private field name and string form of the value
    dbFieldName = self._jobs.pubToDBNameDict[fieldName]

    # Get a database connection and cursor
    with ConnectionFactory.get() as conn:
      query = 'UPDATE %s SET %s=%s+%%s ' \
              '          WHERE job_id=%%s' \
              % (self.jobsTableName, dbFieldName, dbFieldName)
      sqlParams = [increment, jobID]

      if useConnectionID:
        query += ' AND _eng_cjm_conn_id=%s'
        sqlParams.append(self._connectionID)

      result = conn.cursor.execute(query, sqlParams)

    if result != 1:
      raise RuntimeError(
        "Tried to increment the field (%r) of jobID=%s (conn_id=%r), but an " \
        "error occurred. result=%r; query=%r" % (
          dbFieldName, jobID, self._connectionID, result, query))


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def jobUpdateResults(self, jobID, results):
    """ Update the results string and last-update-time fields of a model.

    Parameters:
    ----------------------------------------------------------------
    jobID:      job ID of model to modify
    results:    new results (json dict string)
    """
    with ConnectionFactory.get() as conn:
      query = 'UPDATE %s SET _eng_last_update_time=UTC_TIMESTAMP(), ' \
              '              results=%%s ' \
              '          WHERE job_id=%%s' % (self.jobsTableName,)
      conn.cursor.execute(query, [results, jobID])


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def modelsClearAll(self):
    """ Delete all models from the models table

    Parameters:
    ----------------------------------------------------------------
    """
    self._logger.info('Deleting all rows from models table %r',
                      self.modelsTableName)
    with ConnectionFactory.get() as conn:
      query = 'DELETE FROM %s' % (self.modelsTableName)
      conn.cursor.execute(query)


  ##############################################################################
  @logExceptions(_getLogger)
  def modelInsertAndStart(self, jobID, params, paramsHash, particleHash=None):
    """ Insert a new unique model (based on params) into the model table in the
    "running" state. This will return two things: whether or not the model was
    actually inserted (i.e. that set of params isn't already in the table) and
    the modelID chosen for that set of params. Even if the model was not
    inserted by this call (it was already there) the modelID of the one already
    inserted is returned.

    Parameters:
    ----------------------------------------------------------------
    jobID:            jobID of the job to add models for
    params:           params for this model
    paramsHash        hash of the params, generated by the worker
    particleHash      hash of the particle info (for PSO). If not provided,
                      then paramsHash will be used.

    retval:           (modelID, wasInserted)
                      modelID: the model ID for this set of params
                      wasInserted: True if this call ended up inserting the
                      new model. False if this set of params was already in
                      the model table.
    """
    # Fill in default particleHash
    if particleHash is None:
      particleHash = paramsHash

    # Normalize hashes
    paramsHash = self._normalizeHash(paramsHash)
    particleHash = self._normalizeHash(particleHash)

    def findExactMatchNoRetries(conn):
      return self._getOneMatchingRowNoRetries(
        self._models, conn,
        {'job_id':jobID, '_eng_params_hash':paramsHash,
         '_eng_particle_hash':particleHash},
        ['model_id', '_eng_worker_conn_id'])

    @g_retrySQL
    def findExactMatchWithRetries():
      with ConnectionFactory.get() as conn:
        return findExactMatchNoRetries(conn)

    # Check if the model is already in the models table
    #
    # NOTE: with retries of mysql transient failures, we can't always tell
    #  whether the row was already inserted (e.g., comms failure could occur
    #  after insertion into table, but before arrival or response), so the
    #  need to check before attempting to insert a new row
    #
    # TODO: if we could be assured that the caller already verified the
    #  model's absence before calling us, we could skip this check here
    row = findExactMatchWithRetries()
    if row is not None:
      return (row[0], False)

    @g_retrySQL
    def insertModelWithRetries():
      """ NOTE: it's possible that another process on some machine is attempting
      to insert the same model at the same time as the caller """
      with ConnectionFactory.get() as conn:
        # Create a new job entry
        query = 'INSERT INTO %s (job_id, params, status, _eng_params_hash, ' \
                '  _eng_particle_hash, start_time, _eng_last_update_time, ' \
                '  _eng_worker_conn_id) ' \
                '  VALUES (%%s, %%s, %%s, %%s, %%s, UTC_TIMESTAMP(), ' \
                '          UTC_TIMESTAMP(), %%s) ' \
                % (self.modelsTableName,)
        sqlParams = (jobID, params, self.STATUS_RUNNING, paramsHash,
                     particleHash, self._connectionID)
        try:
          numRowsAffected = conn.cursor.execute(query, sqlParams)
        except Exception, e:
          # NOTE: We have seen instances where some package in the calling
          #  chain tries to interpret the exception message using unicode.
          #  Since the exception message contains binary data (the hashes), this
          #  can in turn generate a Unicode translation exception. So, we catch
          #  ALL exceptions here and look for the string "Duplicate entry" in
          #  the exception args just in case this happens. For example, the 
          #  Unicode exception we might get is:
          #   (<type 'exceptions.UnicodeDecodeError'>, UnicodeDecodeError('utf8', "Duplicate entry '1000-?.\x18\xb1\xd3\xe0CO\x05\x8b\xf80\xd7E5\xbb' for key 'job_id'", 25, 26, 'invalid start byte'))
          # 
          #  If it weren't for this possible Unicode translation error, we 
          #  could watch for only the exceptions we want, like this:  
          #  except pymysql.IntegrityError, e:
          #    if e.args[0] != mysqlerrors.DUP_ENTRY:
          #      raise
          if "Duplicate entry" not in str(e):
            raise
          
          # NOTE: duplicate entry scenario: however, we can't discern
          # whether it was inserted by another process or this one, because an
          # intermittent failure may have caused us to retry
          self._logger.info('Model insert attempt failed with DUP_ENTRY: '
                            'jobID=%s; paramsHash=%s OR particleHash=%s; %r',
                            jobID, paramsHash.encode('hex'),
                            particleHash.encode('hex'), e)
        else:
          if numRowsAffected == 1:
            # NOTE: SELECT LAST_INSERT_ID() returns 0 after re-connection
            conn.cursor.execute('SELECT LAST_INSERT_ID()')
            modelID = conn.cursor.fetchall()[0][0]
            if modelID != 0:
              return (modelID, True)
            else:
              self._logger.warn(
                'SELECT LAST_INSERT_ID for model returned 0, implying loss of '
                'connection: jobID=%s; paramsHash=%r; particleHash=%r',
                jobID, paramsHash, particleHash)
          else:
            self._logger.error(
              'Attempt to insert model resulted in unexpected numRowsAffected: '
              'expected 1, but got %r; jobID=%s; paramsHash=%r; '
              'particleHash=%r',
              numRowsAffected, jobID, paramsHash, particleHash)

        # Look up the model and discern whether it is tagged with our conn id
        row = findExactMatchNoRetries(conn)
        if row is not None:
          (modelID, connectionID) = row
          return (modelID, connectionID == self._connectionID)

        # This set of params is already in the table, just get the modelID
        query = 'SELECT (model_id) FROM %s ' \
                '                  WHERE job_id=%%s AND ' \
                '                        (_eng_params_hash=%%s ' \
                '                         OR _eng_particle_hash=%%s) ' \
                '                  LIMIT 1 ' \
                % (self.modelsTableName,)
        sqlParams = [jobID, paramsHash, particleHash]
        numRowsFound = conn.cursor.execute(query, sqlParams)
        assert numRowsFound == 1, (
          'Model not found: jobID=%s AND (paramsHash=%r OR particleHash=%r); '
          'numRowsFound=%r') % (jobID, paramsHash, particleHash, numRowsFound)
        (modelID,) = conn.cursor.fetchall()[0]
        return (modelID, False)


    return insertModelWithRetries()


  ##############################################################################
  @logExceptions(_getLogger)
  def modelsInfo(self, modelIDs):
    """ Get ALL info for a set of models

    WARNING!!!: The order of the results are NOT necessarily in the same order as
    the order of the model IDs passed in!!!

    Parameters:
    ----------------------------------------------------------------
    modelIDs:    list of model IDs
    retval:      list of nametuples containing all the fields stored for each
                    model.
    """
    assert isinstance(modelIDs, self._SEQUENCE_TYPES), (
      "wrong modelIDs type: %s") % (type(modelIDs),)
    assert modelIDs, "modelIDs is empty"

    rows = self._getMatchingRowsWithRetries(
      self._models, dict(model_id=modelIDs),
      [self._models.pubToDBNameDict[f]
       for f in self._models.modelInfoNamedTuple._fields])

    results = [self._models.modelInfoNamedTuple._make(r) for r in rows]

    # NOTE: assetion will also fail if modelIDs contains duplicates
    assert len(results) == len(modelIDs), "modelIDs not found: %s" % (
      set(modelIDs) - set(r.modelId for r in results))

    return results


  ##############################################################################
  @logExceptions(_getLogger)
  def modelsGetFields(self, modelIDs, fields):
    """ Fetch the values of 1 or more fields from a sequence of model records.
    Here, 'fields' is a list with the names of the fields to fetch. The names
    are the public names of the fields (camelBack, not the lower_case_only form
    as stored in the DB).

    WARNING!!!: The order of the results are NOT necessarily in the same order
    as the order of the model IDs passed in!!!


    Parameters:
    ----------------------------------------------------------------
    modelIDs:      A single modelID or sequence of modelIDs
    fields:        A list  of fields to return

    Returns:  If modelIDs is a sequence:
                a list of tuples->(modelID, [field1, field2,...])
              If modelIDs is a single modelID:
                a list of field values->[field1, field2,...]
    """
    assert len(fields) >= 1, 'fields is empty'

    # Form the sequence of field name strings that will go into the
    #  request
    isSequence = isinstance(modelIDs, self._SEQUENCE_TYPES)

    if isSequence:
      assert len(modelIDs) >=1, 'modelIDs is empty'
    else:
      modelIDs = [modelIDs]

    rows = self._getMatchingRowsWithRetries(
      self._models, dict(model_id=modelIDs),
      ['model_id'] + [self._models.pubToDBNameDict[f] for f in fields])

    if len(rows) < len(modelIDs):
      raise RuntimeError("modelIDs not found within the models table: %s" % (
        (set(modelIDs) - set(r[0] for r in rows)),))

    if not isSequence:
      return list(rows[0][1:])

    return [(r[0], list(r[1:])) for r in rows]


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def modelsGetFieldsForJob(self, jobID, fields, ignoreKilled=False):
    """ Gets the specified fields for all the models for a single job. This is
    similar to modelsGetFields

    Parameters:
    ----------------------------------------------------------------
    jobID:              jobID for the models to be searched
    fields:             A list  of fields to return
    ignoreKilled:       (True/False). If True, this will ignore models that
                        have been killed

    Returns: a (possibly empty) list of tuples as follows
      [
        (model_id1, [field1, ..., fieldn]),
        (model_id2, [field1, ..., fieldn]),
        (model_id3, [field1, ..., fieldn])
                    ...
      ]

    NOTE: since there is a window of time between a job getting inserted into
     jobs table and the job's worker(s) starting up and creating models, an
     empty-list result is one of the normal outcomes.
    """

    assert len(fields) >= 1, 'fields is empty'

    # Form the sequence of field name strings that will go into the
    #  request
    dbFields = [self._models.pubToDBNameDict[x] for x in fields]
    dbFieldsStr = ','.join(dbFields)

    query = 'SELECT model_id, %s FROM %s ' \
              '          WHERE job_id=%%s ' \
              % (dbFieldsStr, self.modelsTableName)
    sqlParams = [jobID]

    if ignoreKilled:
      query += ' AND (completion_reason IS NULL OR completion_reason != %s)'
      sqlParams.append(self.CMPL_REASON_KILLED)

    # Get a database connection and cursor
    with ConnectionFactory.get() as conn:
      conn.cursor.execute(query, sqlParams)
      rows = conn.cursor.fetchall()

    if rows is None:
      # fetchall is defined to return a (possibly-empty) sequence of
      # sequences; however, we occasionally see None returned and don't know
      # why...
      self._logger.error("Unexpected None result from cursor.fetchall; "
                         "query=%r; Traceback=%r",
                         query, traceback.format_exc())

    return [(r[0], list(r[1:])) for r in rows]


  ############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def modelsGetFieldsForCheckpointed(self, jobID, fields):
    """
    Gets fields from all models in a job that have been checkpointed. This is
    used to figure out whether or not a new model should be checkpointed.

    Parameters:
    -----------------------------------------------------------------------
    jobID:                    The jobID for the models to be searched
    fields:                   A list of fields to return

    Returns: a (possibly-empty) list of tuples as follows
      [
        (model_id1, [field1, ..., fieldn]),
        (model_id2, [field1, ..., fieldn]),
        (model_id3, [field1, ..., fieldn])
                    ...
      ]
    """

    assert len(fields) >= 1, "fields is empty"

    # Get a database connection and cursor
    with ConnectionFactory.get() as conn:
      dbFields = [self._models.pubToDBNameDict[f] for f in fields]
      dbFieldStr = ", ".join(dbFields)

      query = 'SELECT model_id, {fields} from {models}' \
              '   WHERE job_id=%s AND model_checkpoint_id IS NOT NULL'.format(
        fields=dbFieldStr, models=self.modelsTableName)

      conn.cursor.execute(query, [jobID])
      rows = conn.cursor.fetchall()

    return [(r[0], list(r[1:])) for r in rows]


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def modelSetFields(self, modelID, fields, ignoreUnchanged = False):
    """ Change the values of 1 or more fields in a model. Here, 'fields' is a
    dict with the name/value pairs to change. The names are the public names of
    the fields (camelBack, not the lower_case_only form as stored in the DB).

    Parameters:
    ----------------------------------------------------------------
    jobID:     jobID of the job record

    fields:    dictionary of fields to change

    ignoreUnchanged: The default behavior is to throw a
    RuntimeError if no rows are affected. This could either be
    because:
      1) Because there was no matching modelID
      2) or if the data to update matched the data in the DB exactly.

    Set this parameter to True if you expect case 2 and wish to
    supress the error.
    """

    # Form the sequence of key=value strings that will go into the
    #  request
    assignmentExpressions = ','.join(
      '%s=%%s' % (self._models.pubToDBNameDict[f],) for f in fields.iterkeys())
    assignmentValues = fields.values()

    query = 'UPDATE %s SET %s, update_counter = update_counter+1 ' \
            '          WHERE model_id=%%s' \
            % (self.modelsTableName, assignmentExpressions)
    sqlParams = assignmentValues + [modelID]

    # Get a database connection and cursor
    with ConnectionFactory.get() as conn:
      numAffectedRows = conn.cursor.execute(query, sqlParams)
      self._logger.debug("Executed: numAffectedRows=%r, query=%r, sqlParams=%r",
                         numAffectedRows, query, sqlParams)

    if numAffectedRows != 1 and not ignoreUnchanged:
      raise RuntimeError(
        ("Tried to change fields (%r) of model %r (conn_id=%r), but an error "
         "occurred. numAffectedRows=%r; query=%r; sqlParams=%r") % (
          fields, modelID, self._connectionID, numAffectedRows, query,
          sqlParams,))


  ##############################################################################
  @logExceptions(_getLogger)
  def modelsGetParams(self, modelIDs):
    """ Get the params and paramsHash for a set of models.

    WARNING!!!: The order of the results are NOT necessarily in the same order as
    the order of the model IDs passed in!!!

    Parameters:
    ----------------------------------------------------------------
    modelIDs:    list of model IDs
    retval:      list of result namedtuples defined in
                  ClientJobsDAO._models.getParamsNamedTuple. Each tuple
                  contains: (modelId, params, engParamsHash)
    """
    assert isinstance(modelIDs, self._SEQUENCE_TYPES), (
      "Wrong modelIDs type: %r") % (type(modelIDs),)
    assert len(modelIDs) >= 1, "modelIDs is empty"

    rows = self._getMatchingRowsWithRetries(
      self._models, {'model_id' : modelIDs},
      [self._models.pubToDBNameDict[f]
       for f in self._models.getParamsNamedTuple._fields])

    # NOTE: assertion will also fail when modelIDs contains duplicates
    assert len(rows) == len(modelIDs), "Didn't find modelIDs: %r" % (
      (set(modelIDs) - set(r[0] for r in rows)),)

    # Return the params and params hashes as a namedtuple
    return [self._models.getParamsNamedTuple._make(r) for r in rows]


  ##############################################################################
  @logExceptions(_getLogger)
  def modelsGetResultAndStatus(self, modelIDs):
    """ Get the results string and other status fields for a set of models.

    WARNING!!!: The order of the results are NOT necessarily in the same order
    as the order of the model IDs passed in!!!

    For each model, this returns a tuple containing:
     (modelID, results, status, updateCounter, numRecords, completionReason,
         completionMsg, engParamsHash

    Parameters:
    ----------------------------------------------------------------
    modelIDs:    list of model IDs
    retval:      list of result tuples. Each tuple contains:
                    (modelID, results, status, updateCounter, numRecords,
                      completionReason, completionMsg, engParamsHash)
    """
    assert isinstance(modelIDs, self._SEQUENCE_TYPES), (
      "Wrong modelIDs type: %r") % type(modelIDs)
    assert len(modelIDs) >= 1, "modelIDs is empty"

    rows = self._getMatchingRowsWithRetries(
      self._models, {'model_id' : modelIDs},
      [self._models.pubToDBNameDict[f]
       for f in self._models.getResultAndStatusNamedTuple._fields])

    # NOTE: assertion will also fail when modelIDs contains duplicates
    assert len(rows) == len(modelIDs), "Didn't find modelIDs: %r" % (
      (set(modelIDs) - set(r[0] for r in rows)),)

    # Return the results as a list of namedtuples
    return [self._models.getResultAndStatusNamedTuple._make(r) for r in rows]


  ##############################################################################
  @logExceptions(_getLogger)
  def modelsGetUpdateCounters(self, jobID):
    """ Return info on all of the models that are in already in the models
    table for a given job. For each model, this returns a tuple
    containing: (modelID, updateCounter).

    Note that we don't return the results for all models, since the results
    string could be quite large. The information we are returning is
    just 2 integer fields.

    Parameters:
    ----------------------------------------------------------------
    jobID:      jobID to query
    retval:     (possibly empty) list of tuples. Each tuple contains:
                  (modelID, updateCounter)
    """
    rows = self._getMatchingRowsWithRetries(
      self._models, {'job_id' : jobID},
      [self._models.pubToDBNameDict[f]
       for f in self._models.getUpdateCountersNamedTuple._fields])

    # Return the results as a list of namedtuples
    return [self._models.getUpdateCountersNamedTuple._make(r) for r in rows]


  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def modelUpdateResults(self, modelID, results=None, metricValue =None,
                         numRecords=None):
    """ Update the results string, and/or num_records fields of
    a model. This will fail if the model does not currently belong to this
    client (connection_id doesn't match).

    Parameters:
    ----------------------------------------------------------------
    modelID:      model ID of model to modify
    results:      new results, or None to ignore
    metricValue:  the value of the metric being optimized, or None to ignore
    numRecords:   new numRecords, or None to ignore
    """

    assignmentExpressions = ['_eng_last_update_time=UTC_TIMESTAMP()',
                             'update_counter=update_counter+1']
    assignmentValues = []

    if results is not None:
      assignmentExpressions.append('results=%s')
      assignmentValues.append(results)

    if numRecords is not None:
      assignmentExpressions.append('num_records=%s')
      assignmentValues.append(numRecords)

    # NOTE1: (metricValue==metricValue) tests for Nan
    # NOTE2: metricValue is being passed as numpy.float64
    if metricValue is not None and (metricValue==metricValue):
      assignmentExpressions.append('optimized_metric=%s')
      assignmentValues.append(float(metricValue))

    query = 'UPDATE %s SET %s ' \
            '          WHERE model_id=%%s and _eng_worker_conn_id=%%s' \
                % (self.modelsTableName, ','.join(assignmentExpressions))
    sqlParams = assignmentValues + [modelID, self._connectionID]

    # Get a database connection and cursor
    with ConnectionFactory.get() as conn:
      numRowsAffected = conn.cursor.execute(query, sqlParams)

    if numRowsAffected != 1:
      raise InvalidConnectionException(
        ("Tried to update the info of modelID=%r using connectionID=%r, but "
         "this model belongs to some other worker or modelID not found; "
         "numRowsAffected=%r") % (modelID,self._connectionID, numRowsAffected,))


  ##############################################################################
  def modelUpdateTimestamp(self, modelID):
    self.modelUpdateResults(modelID)

  ##############################################################################
  @logExceptions(_getLogger)
  @g_retrySQL
  def modelSetCompleted(self, modelID, completionReason, completionMsg,
                        cpuTime=0, useConnectionID=True):
    """ Mark a model as completed, with the given completionReason and
    completionMsg. This will fail if the model does not currently belong to this
    client (connection_id doesn't match).

    Parameters:
    ----------------------------------------------------------------
    modelID:             model ID of model to modify
    completionReason:    completionReason string
    completionMsg:       completionMsg string
    cpuTime:             amount of CPU time spent on this model
    useConnectionID:     True if the connection id of the calling function
                          must be the same as the connection that created the
                          job. Set to True for hypersearch workers, which use
                          this mechanism for orphaned model detection.
    """
    if completionMsg is None:
      completionMsg = ''

    query = 'UPDATE %s SET status=%%s, ' \
              '            completion_reason=%%s, ' \
              '            completion_msg=%%s, ' \
              '            end_time=UTC_TIMESTAMP(), ' \
              '            cpu_time=%%s, ' \
              '            _eng_last_update_time=UTC_TIMESTAMP(), ' \
              '            update_counter=update_counter+1 ' \
              '        WHERE model_id=%%s' \
              % (self.modelsTableName,)
    sqlParams = [self.STATUS_COMPLETED, completionReason, completionMsg,
                 cpuTime, modelID]

    if useConnectionID:
      query += " AND _eng_worker_conn_id=%s"
      sqlParams.append(self._connectionID)

    with ConnectionFactory.get() as conn:
      numRowsAffected = conn.cursor.execute(query, sqlParams)

    if numRowsAffected != 1:
      raise InvalidConnectionException(
        ("Tried to set modelID=%r using connectionID=%r, but this model "
         "belongs to some other worker or modelID not found; "
         "numRowsAffected=%r") % (modelID, self._connectionID, numRowsAffected))


  ##############################################################################
  @logExceptions(_getLogger)
  def modelAdoptNextOrphan(self, jobId, maxUpdateInterval):
    """ Look through the models table for an orphaned model, which is a model
    that is not completed yet, whose _eng_last_update_time is more than
    maxUpdateInterval seconds ago.

    If one is found, change its _eng_worker_conn_id to the current worker's
    and return the model id.

    Parameters:
    ----------------------------------------------------------------
    retval:    modelId of the model we adopted, or None if none found
    """

    @g_retrySQL
    def findCandidateModelWithRetries():
      modelID = None
      with ConnectionFactory.get() as conn:
        # TODO: may need a table index on job_id/status for speed
        query = 'SELECT model_id FROM %s ' \
                '   WHERE  status=%%s ' \
                '          AND job_id=%%s ' \
                '          AND TIMESTAMPDIFF(SECOND, ' \
                '                            _eng_last_update_time, ' \
                '                            UTC_TIMESTAMP()) > %%s ' \
                '   LIMIT 1 ' \
                % (self.modelsTableName,)
        sqlParams = [self.STATUS_RUNNING, jobId, maxUpdateInterval]
        numRows = conn.cursor.execute(query, sqlParams)
        rows = conn.cursor.fetchall()

      assert numRows <= 1, "Unexpected numRows: %r" % numRows
      if numRows == 1:
        (modelID,) = rows[0]

      return modelID

    @g_retrySQL
    def adoptModelWithRetries(modelID):
      adopted = False
      with ConnectionFactory.get() as conn:
        query = 'UPDATE %s SET _eng_worker_conn_id=%%s, ' \
                  '            _eng_last_update_time=UTC_TIMESTAMP() ' \
                  '        WHERE model_id=%%s ' \
                  '              AND status=%%s' \
                  '              AND TIMESTAMPDIFF(SECOND, ' \
                  '                                _eng_last_update_time, ' \
                  '                                UTC_TIMESTAMP()) > %%s ' \
                  '        LIMIT 1 ' \
                  % (self.modelsTableName,)
        sqlParams = [self._connectionID, modelID, self.STATUS_RUNNING,
                     maxUpdateInterval]
        numRowsAffected = conn.cursor.execute(query, sqlParams)

        assert numRowsAffected <= 1, 'Unexpected numRowsAffected=%r' % (
          numRowsAffected,)

        if numRowsAffected == 1:
          adopted = True
        else:
          # Discern between transient failure during update and someone else
          # claiming this model
          (status, connectionID) = self._getOneMatchingRowNoRetries(
            self._models, conn, {'model_id':modelID},
            ['status', '_eng_worker_conn_id'])
          adopted = (status == self.STATUS_RUNNING and
                     connectionID == self._connectionID)
      return adopted


    adoptedModelID = None
    while True:
      modelID = findCandidateModelWithRetries()
      if modelID is None:
        break
      if adoptModelWithRetries(modelID):
        adoptedModelID = modelID
        break

    return adoptedModelID



###############################################################################
#def testClientJobsDAO():
#  # WARNING: these tests assume that Nupic Scheduler is not running, and bad
#  #  things will happen if the test is executed while the Scheduler is running
#
#  # TODO: This test code is out of date: e.g., at the time of this writing,
#  #  jobStartNext() advances a job's status to STATUS_RUNNING instead of
#  #  STATUS_STARTING; etc.
#
#  import time
#  import hashlib
#  import pprint
#
#  # Clear out the database
#  cjDAO = ClientJobsDAO.get()
#  cjDAO.connect(deleteOldVersions=True, recreate=True)
#
#
#  # --------------------------------------------------------------------
#  # Test inserting a new job that doesn't have to be unique
#  jobID1 = cjDAO.jobInsert(client='test', cmdLine='echo hi',
#              clientInfo='client info', params='job params')
#  print "Inserted job %d" % (jobID1)
#
#  jobID2 = cjDAO.jobInsert(client='test', cmdLine='echo hi',
#              clientInfo='client info', params='job params')
#  print "Inserted job %d" % (jobID2)
#
#
#  # --------------------------------------------------------------------
#  # Test starting up those jobs
#  jobID = cjDAO.jobStartNext()
#  print "started job %d" % (jobID)
#  assert (jobID == jobID1)
#  info = cjDAO.jobInfo(jobID)
#  print "jobInfo:"
#  pprint.pprint(info)
#  assert (info.status == cjDAO.STATUS_STARTING)
#
#  jobID = cjDAO.jobStartNext()
#  print "started job %d" % (jobID)
#  assert (jobID == jobID2)
#  info = cjDAO.jobInfo(jobID)
#  print "jobInfo:"
#  pprint.pprint(info)
#  assert (info.status == cjDAO.STATUS_STARTING)
#
#
#  # --------------------------------------------------------------------
#  # Test inserting a unique job
#  jobHash = '01234'
#  (success, jobID3) = cjDAO.jobInsertUnique(client='testuniq',
#              cmdLine='echo hi',
#              jobHash=jobHash, clientInfo='client info', params='job params')
#  print "Inserted unique job %d" % (jobID3)
#  assert (success)
#
#  # This should return the same jobID
#  (success, jobID4) = cjDAO.jobInsertUnique(client='testuniq',
#              cmdLine='echo hi',
#              jobHash=jobHash, clientInfo='client info', params='job params')
#  print "tried to insert again %d" % (jobID4)
#  assert (not success and jobID4 == jobID3)
#
#
#  # Mark it as completed
#  jobID = cjDAO.jobStartNext()
#  assert (jobID == jobID3)
#  cjDAO.jobSetStatus(jobID3, cjDAO.STATUS_COMPLETED)
#
#
#  # This should return success
#  (success, jobID4) = cjDAO.jobInsertUnique(client='testuniq',
#              cmdLine='echo hi',
#              jobHash=jobHash, clientInfo='client info', params='job params')
#  print "Inserted unique job %d" % (jobID4)
#  assert (success)
#
#
#  # --------------------------------------------------------------------
#  # Test inserting a pre-started job
#  jobID5 = cjDAO.jobInsert(client='test', cmdLine='echo hi',
#              clientInfo='client info', params='job params',
#              alreadyRunning=True)
#  print "Inserted prestarted job %d" % (jobID5)
#
#  info = cjDAO.jobInfo(jobID5)
#  print "jobInfo:"
#  pprint.pprint(info)
#  assert (info.status == cjDAO.STATUS_TESTMODE)
#
#
#
#  # --------------------------------------------------------------------
#  # Test the jobInfo and jobSetFields calls
#  jobInfo = cjDAO.jobInfo(jobID2)
#  print "job info:"
#  pprint.pprint(jobInfo)
#  newFields = dict(maximumWorkers=43)
#  cjDAO.jobSetFields(jobID2, newFields)
#  jobInfo = cjDAO.jobInfo(jobID2)
#  assert(jobInfo.maximumWorkers == newFields['maximumWorkers'])
#
#
#  # --------------------------------------------------------------------
#  # Test the jobGetFields call
#  values = cjDAO.jobGetFields(jobID2, ['maximumWorkers'])
#  assert (values[0] == newFields['maximumWorkers'])
#
#
#  # --------------------------------------------------------------------
#  # Test the jobSetFieldIfEqual call
#  values = cjDAO.jobGetFields(jobID2, ['engWorkerState'])
#  assert (values[0] == None)
#
#  # Change from None to test
#  success = cjDAO.jobSetFieldIfEqual(jobID2, 'engWorkerState',
#                newValue='test', curValue=None)
#  assert (success)
#  values = cjDAO.jobGetFields(jobID2, ['engWorkerState'])
#  assert (values[0] == 'test')
#
#  # Change from test1 to test2 (should fail)
#  success = cjDAO.jobSetFieldIfEqual(jobID2, 'engWorkerState',
#                newValue='test2', curValue='test1')
#  assert (not success)
#  values = cjDAO.jobGetFields(jobID2, ['engWorkerState'])
#  assert (values[0] == 'test')
#
#  # Change from test to test2
#  success = cjDAO.jobSetFieldIfEqual(jobID2, 'engWorkerState',
#                newValue='test2', curValue='test')
#  assert (success)
#  values = cjDAO.jobGetFields(jobID2, ['engWorkerState'])
#  assert (values[0] == 'test2')
#
#  # Change from test2 to None
#  success = cjDAO.jobSetFieldIfEqual(jobID2, 'engWorkerState',
#                newValue=None, curValue='test2')
#  assert (success)
#  values = cjDAO.jobGetFields(jobID2, ['engWorkerState'])
#  assert (values[0] == None)
#
#
#  # --------------------------------------------------------------------
#  # Test job demands
#  jobID6 = cjDAO.jobInsert(client='test', cmdLine='echo hi',
#              clientInfo='client info', params='job params',
#              minimumWorkers=1, maximumWorkers=1,
#              alreadyRunning=False)
#  jobID7 = cjDAO.jobInsert(client='test', cmdLine='echo hi',
#              clientInfo='client info', params='job params',
#              minimumWorkers=4, maximumWorkers=10,
#              alreadyRunning=False)
#  cjDAO.jobSetStatus(jobID6, ClientJobsDAO.STATUS_RUNNING,
#                     useConnectionID=False,)
#  cjDAO.jobSetStatus(jobID7, ClientJobsDAO.STATUS_RUNNING,
#                     useConnectionID=False,)
#  jobsDemand = cjDAO.jobGetDemand()
#  assert (jobsDemand[0].minimumWorkers==1 and jobsDemand[0].maximumWorkers==1)
#  assert (jobsDemand[1].minimumWorkers==4 and jobsDemand[1].maximumWorkers==10)
#  assert (jobsDemand[0].engAllocateNewWorkers == True and \
#          jobsDemand[0].engUntendedDeadWorkers == False)
#
#  # Test increment field
#  values = cjDAO.jobGetFields(jobID7, ['numFailedWorkers'])
#  assert (values[0] == 0)
#  cjDAO.jobIncrementIntField(jobID7, 'numFailedWorkers', 1)
#  values = cjDAO.jobGetFields(jobID7, ['numFailedWorkers'])
#  assert (values[0] == 1)
#
#  # --------------------------------------------------------------------
#  # Test inserting new models
#
#  params = "params1"
#  hash1 = hashlib.md5(params).digest()
#  (modelID1, ours) = cjDAO.modelInsertAndStart(jobID, params, hash1)
#  print "insert %s,%s:" % (params, hash1.encode('hex')), modelID1, ours
#  assert (ours)
#
#  params = "params2"
#  hash2 = hashlib.md5(params).digest()
#  (modelID2, ours) = cjDAO.modelInsertAndStart(jobID, params, hash2)
#  print "insert %s,%s:" % (params, hash2.encode('hex')), modelID2, ours
#  assert (ours)
#
#  params = "params3"
#  hash3 = hashlib.md5(params).digest()
#  (modelID3, ours) = cjDAO.modelInsertAndStart(jobID, params, hash3)
#  print "insert %s,%s:" % (params, hash3.encode('hex')), modelID3, ours
#  assert (ours)
#
#  params = "params4"
#  hash4 = hashlib.md5(params).digest()
#  (modelID4, ours) = cjDAO.modelInsertAndStart(jobID, params, hash4)
#  print "insert %s,%s:" % (params, hash4.encode('hex')), modelID4, ours
#  assert (ours)
#
#  params = "params5"
#  hash5 = hashlib.md5(params).digest()
#  (modelID5, ours) = cjDAO.modelInsertAndStart(jobID, params, hash5)
#  print "insert %s,%s:" % (params, hash5.encode('hex')), modelID5, ours
#  assert (ours)
#
#
#  # Try to insert the same model again
#  params = "params2"
#  hash = hashlib.md5(params).digest()
#  (modelID, ours) = cjDAO.modelInsertAndStart(jobID, params, hash)
#  print "insert %s,%s:" % (params, hash.encode('hex')), modelID, ours
#  assert (not ours and modelID == modelID2)
#
#
#  # ---------------------------------------------------------------
#  # Test inserting models with unique particle hashes
#  params = "params6"
#  paramsHash = hashlib.md5(params).digest()
#  particle = "particle6"
#  particleHash = hashlib.md5(particle).digest()
#  (modelID6, ours) = cjDAO.modelInsertAndStart(jobID, params, paramsHash,
#                                              particleHash)
#  print "insert %s,%s,%s:" % (params, paramsHash.encode('hex'),
#                              particleHash.encode('hex')), modelID6, ours
#  assert (ours)
#
#  # Should fail if we insert with the same params hash
#  params = "params6"
#  paramsHash = hashlib.md5(params).digest()
#  particle = "particleUnique"
#  particleHash = hashlib.md5(particle).digest()
#  (modelID, ours) = cjDAO.modelInsertAndStart(jobID, params, paramsHash,
#                                              particleHash)
#  print "insert %s,%s,%s:" % (params, paramsHash.encode('hex'),
#                              particleHash.encode('hex')), modelID6, ours
#  assert (not ours and modelID == modelID6)
#
#  # Should fail if we insert with the same particle hash
#  params = "paramsUnique"
#  paramsHash = hashlib.md5(params).digest()
#  particle = "particle6"
#  particleHash = hashlib.md5(particle).digest()
#  (modelID, ours) = cjDAO.modelInsertAndStart(jobID, params, paramsHash,
#                                              particleHash)
#  print "insert %s,%s,%s:" % (params, paramsHash.encode('hex'),
#                              particleHash.encode('hex')), modelID6, ours
#  assert (not ours and modelID == modelID6)
#
#
#
#  # --------------------------------------------------------------------
#  # Test getting params for a set of models
#  paramsAndHash = cjDAO.modelsGetParams([modelID1, modelID2])
#  print "modelID, params, paramsHash of %s:" % ([modelID1, modelID2])
#  for (modelID, params, hash) in paramsAndHash:
#    print "  ", modelID, params, hash.encode('hex')
#    if modelID == modelID1:
#      assert (params == "params1" and hash == hash1)
#    elif modelID == modelID2:
#      assert (params == "params2" and hash == hash2)
#    else:
#      assert (false)
#
#
#  # Set some to notstarted
#  #cjDAO.modelUpdateStatus(modelID2, status=cjDAO.STATUS_NOTSTARTED)
#  #cjDAO.modelUpdateStatus(modelID3, status=cjDAO.STATUS_NOTSTARTED)
#
#
#  # --------------------------------------------------------------------
#  # Test Update model info
#  cjDAO.modelUpdateResults(modelID2, results="hi there")
#  cjDAO.modelUpdateResults(modelID3, numRecords=100)
#  cjDAO.modelUpdateResults(modelID3, numRecords=110)
#  cjDAO.modelUpdateResults(modelID4, results="bye", numRecords=42)
#  cjDAO.modelUpdateResults(modelID5, results="hello", numRecords=4)
#
#
#  # Test setCompleted
#  cjDAO.modelSetCompleted(modelID5, completionReason=cjDAO.CMPL_REASON_EOF,
#                          completionMsg="completion message")
#
#  # --------------------------------------------------------------------------
#  # Test the GetResultsAndStatus call
#  results = cjDAO.modelsGetResultAndStatus([modelID1, modelID2, modelID3,
#                                            modelID4, modelID5])
#  assert (len(results) == 5)
#  for (modelID, results, status, updateCounter, numRecords,
#       completionReason, completionMsg, engParamsHash,
#       engMatured) in results:
#    if modelID == modelID1:
#      assert (status == cjDAO.STATUS_RUNNING)
#      assert (updateCounter == 0)
#    elif modelID == modelID2:
#      assert (results == 'hi there')
#      assert (updateCounter == 1)
#    elif modelID == modelID3:
#      assert (numRecords == 110)
#      assert (updateCounter == 2)
#    elif modelID == modelID4:
#      assert (updateCounter == 1)
#      assert (results == 'bye')
#      assert (numRecords == 42)
#    elif modelID == modelID5:
#      assert (updateCounter == 2)
#      assert (results == 'hello')
#      assert (numRecords == 4)
#      assert (status == cjDAO.STATUS_COMPLETED)
#      assert (completionReason == cjDAO.CMPL_REASON_EOF)
#      assert (completionMsg == "completion message")
#    else:
#      assert (False)
#
#  # --------------------------------------------------------------------------
#  # Test the ModelsInfo call
#  mInfos = cjDAO.modelsInfo([modelID1, modelID2, modelID3,
#                                            modelID4, modelID5])
#  assert (len(results) == 5)
#  for info in mInfos:
#    modelID = info.modelId
#    if modelID == modelID1:
#      assert (info.status == cjDAO.STATUS_RUNNING)
#      assert (info.updateCounter == 0)
#    elif modelID == modelID2:
#      assert (info.results == 'hi there')
#      assert (info.updateCounter == 1)
#    elif modelID == modelID3:
#      assert (info.numRecords == 110)
#      assert (info.updateCounter == 2)
#    elif modelID == modelID4:
#      assert (info.updateCounter == 1)
#      assert (info.results == 'bye')
#      assert (info.numRecords == 42)
#    elif modelID == modelID5:
#      assert (info.updateCounter == 2)
#      assert (info.results == 'hello')
#      assert (info.numRecords == 4)
#      assert (info.status == cjDAO.STATUS_COMPLETED)
#      assert (info.completionReason == cjDAO.CMPL_REASON_EOF)
#      assert (info.completionMsg == "completion message")
#    else:
#      assert (False)
#
#
#  # Test the GetUpdateCounters call
#  results = cjDAO.modelsGetUpdateCounters(jobID)
#  print " all models update counters:", results
#  expResults = set(((modelID1, 0), (modelID2, 1), (modelID3, 2),
#                   (modelID4, 1), (modelID5, 2), (modelID6, 0)))
#  diff = expResults.symmetric_difference(results)
#  assert (len(diff) == 0)
#
#
#  # -------------------------------------------------------------------
#  # Test the model orphan logic
#  for modelID in [modelID1, modelID2, modelID3, modelID4, modelID5, modelID6]:
#    cjDAO.modelUpdateResults(modelID, results="hi there")
#  orphanedModel = cjDAO.modelAdoptNextOrphan(jobID, maxUpdateInterval=10.0)
#  if orphanedModel is not None:
#    print "Unexpected orphan: ", orphanedModel
#  assert (orphanedModel is None)
#  print "Waiting 2 seconds for model to expire..."
#  time.sleep(2)
#  orphanedModel = cjDAO.modelAdoptNextOrphan(jobID, maxUpdateInterval=1.0)
#  assert (orphanedModel is not None)
#  print "Adopted model", orphanedModel
#
#  print "\nAll tests pass."



helpString = \
"""%prog [options]
This script runs the ClientJobsDAO as a command line tool, for executing
unit tests or for obtaining specific information about the ClientJobsDAO
required for code written in languages other than python.
"""

################################################################################
if __name__ == "__main__":
  """
  Launch the ClientJobsDAO from the command line. This can be done to obtain
  specific information about the ClientJobsDAO when languages other than python
  (i.e. Java) are used.
  """
  # Parse command line options
  parser = OptionParser(helpString)

  parser.add_option("--getDBName", action="store_true", default=False,
        help="Print the name of the database that will be used to stdout "
              " [default: %default]")


  (options, args) = parser.parse_args(sys.argv[1:])
  if len(args) > 0:
    parser.error("Didn't expect any arguments.")


  # Print DB name?
  if options.getDBName:
    cjDAO = ClientJobsDAO()
    print cjDAO.dbName
