# This file is part of PyBOSSA.
# 
# PyBOSSA is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# PyBOSSA is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public License
# along with PyBOSSA.  If not, see <http://www.gnu.org/licenses/>.

import os 
from glob import iglob
import logging
import datetime
import time
import json
import uuid

from werkzeug import generate_password_hash, check_password_hash
import flaskext.login
from sqlalchemy import create_engine
from sqlalchemy import Integer, Unicode, Float, UnicodeText, Text, String
from sqlalchemy.schema import Table, MetaData, Column, ForeignKey
from sqlalchemy.orm import relationship, backref, class_mapper
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.types import MutableType, TypeDecorator

from pybossa.util import pretty_date

log = logging.getLogger(__name__)

Session = scoped_session(sessionmaker())

def set_engine(engine):
    Base.metadata.bind = engine

def make_timestamp():
    now = datetime.datetime.now()
    return now.isoformat()

def make_uuid():
    return str(uuid.uuid4())

def rebuild_db():
    Base.metadata.drop_all()
    Base.metadata.create_all()

# =========================================
# Basics

class JSONType(MutableType, TypeDecorator):
    '''Additional Database Type for handling JSON values.
    '''
    impl = Text

    def __init__(self):
        super(JSONType, self).__init__()

    def process_bind_param(self, value, dialect):
        return json.dumps(value)

    def process_result_value(self, value, dialiect):
        return json.loads(value)

    def copy_value(self, value):
        return json.loads(json.dumps(value))



class StateEnum:
    '''When creating  a task, the task can have the following states::
    
       * ALL: First time created
       * IN_PROGRESS:  The task is being run by one user
       * PENDING: The task has been completed but need to be validated
       * VALID: The task has been completed and validated
       * INVALID: The task has been complete but it is invalid
       * ERROR: The task has an error
    '''
    ALL =  u'all'
    IN_PROGRESS = u'in_progress'
    PENDING = u'pending'
    VALID = u'valid'
    INVALID = u'invalid'
    ERROR = u'error'


class DomainObject(object):
    def dictize(self):
        out = {}
        for col in self.__table__.c:
            out[col.name] = getattr(self, col.name)
        return out

    @classmethod
    def undictize(cls, dict_):
        raise NotImplementedError()

    def __str__(self):
        return self.__unicode__().encode('utf8')

    def __unicode__(self):
        repr = u'<%s' % self.__class__.__name__
        table = class_mapper(self.__class__).mapped_table
        for col in table.c:
            try:
                repr += u' %s=%s' % (col.name, getattr(self, col.name))
            except Exception, inst:
                repr += u' %s=%s' % (col.name, inst)
                
        repr += '>'
        return repr
    

Base = declarative_base(cls=DomainObject)


# =========================================
# Domain Objects

class App(Base):
    '''A microtasking Application to which Tasks are associated.
    '''
    __tablename__ = 'app'
    #: Unique id for this app (autogenerated)
    id                  = Column(Integer, primary_key=True)
    #: created timestamp (automatically set)
    created             = Column(Text, default=make_timestamp)
    #: Name / Title for this Application
    name                = Column(Unicode(length=255), unique=True)
    #: slug used in urls etc
    short_name          = Column(Unicode(length=255), unique=True)
    #: description
    description         = Column(Unicode(length=255))
    #: TODO: What is this?
    long_tasks           = Column(Integer)
    #: Boolean integer (0,1) indicating that this App should be hidden from everyone but Administrators
    hidden              = Column(Integer)
    #: owner (id)
    owner_id            = Column(Integer, ForeignKey('user.id'))
    
    ## Following may not be relevant ...
    ## TODO: ask about these
    #: estimate of time it should take for user
    time_estimate       = Column(Integer)
    #: time limit for a task
    time_limit          = Column(Integer)
    calibration_frac    = Column(Float)
    bolt_course_id      = Column(Integer)
    #: Standard JSON blob for additional data
    info                = Column(JSONType, default=dict)

    ## Relationships
    #: `Task`s for this app.`
    tasks = relationship('Task', backref='app')
    #: `TaskRun`s for this app.`
    task_runs = relationship('TaskRun', backref='app',
                             order_by='TaskRun.finish_time.desc()')

    #: Percentage of completed tasks based on Task.state 
    #: (0 not done, 1 completed)
    def completion_status(self):
        completed = 0
        for task_run in self.task_runs:
                completed += 1
        if len(self.tasks) != 0:
            return float(completed)/len(self.tasks)
        else:
            return float(0)

    def last_activity(self):
        if (len(self.task_runs) >= 1):
            return pretty_date(self.task_runs[0].finish_time)
            #return self.task_runs[0].finish_time
        else:
            return "None"


class Task(Base):
    '''An individual Task which can be performed by a user. A Task is
    associated to an App.
    '''
    __tablename__ = 'task'
    #: unique id (automatically generated)
    id                  = Column(Integer, primary_key=True)
    #: created timestamp (automatically set)
    created             = Column(Text, default=make_timestamp)
    #: ForeignKey to App.id (NB: use task relationship rather than this field
    #: in normal use
    app_id              = Column(Integer, ForeignKey('app.id'))
    #: a StateEnum instance
    # TODO: state should be an integer?
    state               = Column(UnicodeText)
    #: Quorum (number of times this task should be done by different users)
    quorum              = Column(Integer)
    #: Boolean indicating whether this is a calibration Task or not.
    calibration         = Column(Integer)
    #: Value between 0 and 1 indicating priority of task within App (higher = more important)
    priority_0          = Column(Float)
    #: all configuration / details of the Task is stored in info which is an arbitrary JSON object. (Usually expected to be a hash/dict)
    #: For example for an image classification project this would be::
    #: 
    #:    {
    #:       url: [image-url],
    #:       question: [is this a person]
    #:    }
    info                = Column(JSONType, default=dict)

    ## Relationships
    #: `TaskRun`s for this task`
    task_runs = relationship('TaskRun', backref='task')


class TaskRun(Base):
    '''A run of a given task by a specific user.
    '''
    
    __tablename__ = 'task_run'
    #: id
    id                  = Column(Integer, primary_key=True)
    #: created timestamp (automatically set)
    created             = Column(Text, default=make_timestamp)
    #: application id of this task run
    app_id              = Column(Integer, ForeignKey('app.id'))
    #: task id of this task run
    task_id              = Column(Integer, ForeignKey('task.id'))
    #: user id of performer of this task
    user_id             = Column(Integer, ForeignKey('user.id'))
    # ip address of this user (only if anonymous)
    user_ip             = Column(Text)
    #: finish time (iso8601 formatted string)
    finish_time         = Column(Text, default=make_timestamp)

    #: timeout for task
    timeout             = Column(Integer)
    #: See same attribute in Task
    calibration         = Column(Integer)
    
    info                = Column(JSONType, default=dict)
    '''General writable field that should be used by clients to record results of a TaskRun. Usually a template for this will be provided by Task
     
    For example::
    
        result: {
            whatever information shoudl be recorded -- up to task presenter
        }
    '''

class User(Base, flaskext.login.UserMixin):
    __tablename__ = 'user'
    id             = Column(Integer, primary_key=True)
    #: created timestamp (automatically set)
    created             = Column(Text, default=make_timestamp)
    #: email address ...
    email_addr          = Column(Unicode(length=254), unique=True)
    #: user name
    name                = Column(Unicode(length=254), unique=True)
    #: full name
    fullname            = Column(Unicode(length=500))
    #: api key
    api_key             = Column(String(length=36), default=make_uuid, unique=True)
    passwd_hash         = Column(Unicode(length=254), unique=True)
    #: TODO: find out ... bossa specific
    category            = Column(Integer)
    #: TODO: find out ...
    flags               = Column(Integer)
    # Twitter user_id field
    twitter_user_id     = Column(Integer, unique=True)
    #: arbitrary additional information about the user in a JSON dict.
    info                = Column(JSONType, default=dict)

    def get_id(self):
        '''id for login system. equates to name'''
        return self.name

    def set_password(self, password):
        self.passwd_hash  = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.passwd_hash, password)

    @classmethod
    def by_name(cls, name):
        '''Lookup user by (user)name.'''
        return Session.query(User).filter_by(name=name).first()

    ## Relationships
    #: `Task`s for this user
    task_runs = relationship('TaskRun', backref='user')
    apps = relationship('App', backref='owner')
