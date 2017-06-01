#!/usr/bin/env python2
# -*- coding: utf-8 -*-
#
#  Copyright 2012 Unknown <diogo@arch>
#  
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#  
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#  
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.

import numpy as np
import pandas as pd
from collections import Counter, defaultdict, OrderedDict
import itertools
import re
import os
from os.path import join, basename, splitext, exists
from itertools import compress
from threading import Lock
import functools
import sqlite3

# TriFusion imports

try:
    import process
    from process.base import dna_chars, aminoacid_table, iupac, \
        iupac_rev, iupac_conv, Base
    from process.data import Partitions
    from process.data import PartitionException
    from process.error_handling import DuplicateTaxa, KillByUser, \
        InvalidSequenceType, EmptyData, InputError, EmptyAlignment, \
        MultipleSequenceTypes, SingleAlignment
except ImportError:
    import trifusion.process as process
    from trifusion.process.base import dna_chars, aminoacid_table, iupac, \
        iupac_rev, iupac_conv, Base
    from trifusion.process.data import Partitions
    from trifusion.process.data import PartitionException
    from trifusion.process.error_handling import DuplicateTaxa, KillByUser, \
        InvalidSequenceType, EmptyData, InputError, EmptyAlignment, \
        MultipleSequenceTypes, SingleAlignment

# import pickle
# TODO: Create a SequenceSet class for sets of sequences that do not conform
# to an alignment, i.e. unequal length.This would eliminate the problems of
# applying methods designed for alignments to sets of sequences with unequal
# length would allows these sets of sequences to have methods of their own.
# However, the __init__ of the current Alignment object should apply to both
# SequenceSet and Alignment classes. So, I'll have to re-structure the code
# somehow.
# TODO After creating the SequenceSet class, an additional class should be
# used to make the triage of files to either the Alignment or SequenceSet
# classes

# Lock mechanism to prevent concurrent access to sqlite database
lock = Lock()


class LookupDatabase(object):
    """Decorator handling hash lookup table with pre-calculated values.
    
    This decorator class is used to decorate class methods that calculate 
    pairwise sequence similarity. To ensure proper functionality, the
    decorated method should be called first with a single "connect" argument
    and after finishing all calculations, with a single "disconnect" argument.
    These special method callings are necessary to setup and close the
    database storing the calculated values, respectively.
    
    Parameters
    ----------
    func : function
        Decorated function
    
    Attributes
    ----------
    func : function
        Decorated function
    con : None or sqlite.Connection
        Connection object of the sqlite database
    c : None or sqlite.Cursor
        Cursor object of the sqlite database
    
    Notes
    -----
    The `con` and `c` attributes are initialized when the decorated function
    is called with the single "connect" argument 
    (e.g. decorated_func("connect")). When the decorated function is called
    with the single "disconnect" argument (e.g. decorated_func("disconnect")),
    the database changes are committed, the sqlite Connection is closed and
    the Cursor is reset.
    """

    def __init__(self, func):

        self.func = func
        self.con = None
        self.c = None

    def __call__(self, *args):
        """Wraps the call of `func`.
        
        When the decorated method is called, this code is wrapped around
        its execution. It accepts an arbitrary number of positional arguments
        but is is currently designed to decorate the `_get_similarity`
        method of the `AlignmentList` class. There are three expected calling
        modes:
        
            1. decorated_func("connect") : Initializes the Connection and
            Cursor objects of the database
            2. decorated_func(seq1, seq2, locus_lenght) : The typical main
            execution of the method, providing the sequence1 and sequence2
            strings along with the integer with their lenght
            3. decorated_func("disconnect") : Commits changes to database
            and closes Connection and Cursor objects.
        
        The path of this sqlite database is automatically obtained from 
        the `AlignmentList` attribute `sql_path`. We use the path to the 
        same directory, but with a different name for the database, "pw.db".
        
        Parameters
        ----------
        args : list
            List of positional arguments for decorated method
        
        Notes
        -----
        When the decorated method is called normally (i.e., not with a 
        "connect" or "disconnect" argument"), it first creates an hash
        of the argument list (excluding `self`). This hash is used to query
        the database to check if a value for that combination of sequence
        strings and sequence length has already been calculated. If yes, 
        the result is immediately  returned without executing the decorated
        method. If no, the method is executed to perform calculations. The
        result of this calculation is then stored in the database and the 
        result is returned.

        """

        c_args = args[1:3]

        # Initialize database connection.
        if c_args[0] == "connect":

            # Get path to database based on the AlignmentList db
            tmp_dir = os.path.dirname(args[0].sql_path)

            self.con = sqlite3.connect(join(tmp_dir, "pw.db"))
            self.c = self.con.cursor()
            self.c.execute("PRAGMA synchronous = OFF")

            # Create table, if it does not yet exist
            if not self.c.execute("SELECT name FROM sqlite_master WHERE type="
                                  "'table' AND name='pw_table'").fetchall():
                self.c.execute("CREATE TABLE pw_table (hash integer"
                               ", seq_len integer"
                               ", val float, "
                               "effective_len float,"
                               "PRIMARY KEY (hash, seq_len))")

            return

        elif c_args[0] == "disconnect":

            self.con.commit()
            self.con.close()
            self.c = None

            return

        if self.c:

            h = hash(c_args)
            self.c.execute("SELECT * FROM pw_table WHERE hash = ?"
                           " AND seq_len = ?", (h, args[-1]))
            vals = self.c.fetchone()
            if vals:
                return vals[2], vals[3]
            else:
                value = self.func(*args)
                h = hash(c_args)
                self.c.execute("INSERT INTO pw_table VALUES (?, ?, ?, ?)",
                               (h, args[-1], value[0], value[1]))
                return value

        else:
            return self.func(*args)

    def __repr__(self):
        """ Return the function's docstring. """
        return self.func.__doc__

    def __get__(self, obj, objtype):
        """ Support instance methods """
        return functools.partial(self.__call__, obj)


def check_data(func):
    """Decorator handling the result from AlignmentList plotting methods.
    
    This should decorate all plotting methods from the `AlignmentList` class.
    It can be used to control warnings and handle exceptions when calling
    the plotting methods. Currently, it ensures that a list of methods are
    not executed when the `AlignmentList` instance contains a single 
    alignment, and handles cases where the plotting methods return an
    empty array of data. Further control can be added here to prevent
    methods from being executed in certain conditions and handling 
    certain outputs of the plotting methods.
    
    The only requirement is that, even when the plotting methods are not
    executed, this should always return a dictionary. In such cases,
    this dictionary should contain a single key:value with
    {"exception":<exception_type_string>}. The <exception_type_string> should
    be a simple string with an informative name that will be handled in
    the `stats_write_plot` of the `TriFusionApp` class.
    
    Parameters
    ----------
    func : function
        Decorated function

    Returns
    -------
    res : dict
        The value returned by `func` is always a dictionary. If the
        decorator prevents the execution of `func` for some reason, ensure
        that a dictionary is always return with a single key:value
        {"exception": <exception_type_string>}
    """

    # functools.wraps(func) allows the correct generation of docstrings
    # by sphinx of the decorated objects.
    @functools.wraps(func)
    def wrapper(*args, **kwargs):

        # Plot methods that should not be allowed to continue with only one
        #  active alignment
        no_single_plot = ["outlier_missing_data", "outlier_missing_data_sp",
                          "outlier_segregating", "outlier_segregating_sp",
                          "outlier_sequence_size", "outlier_sequence_size_sp",
                          "average_seqsize_per_species", "average_seqsize",
                          "sequence_similarity", "sequence_segregation",
                          "length_polymorphism_correlation",
                          "taxa_distribution", "cumulative_missing_genes",
                          "gene_occupancy", "missing_data_distribution",
                          "missing_genes_average"]

        # Calling outlier method with a single alignments should immediately
        # raise an exception
        if len(args[0].alignments) == 1:
            if func.__name__ in no_single_plot:
                return {"exception": "single_alignment"}

        res = func(*args, **kwargs)

        if "data" in res:
            if np.asarray(res["data"]).any():
                return res
            else:
                return {"exception": "empty_data"}
        elif "exception" in res:
            return res

    return wrapper


def setup_database(func):
    """Decorator handling the active database tables.
    
    Decorates methods from the `Alignment` object that use and
    perform modifications to the original alignment data. All methods 
    decorated with this must have the keyword arguments `table_in` and
    `table_out` (and `use_main_table`, optionally). The values associated with 
    these arguments will determine which tables will be used and created 
    before the execution of the decorated method (See Notes for the 
    rationale). The strings provided as arguments for `table_in` and 
    `table_out` will serve as a suffix to the `Alignment` instance attribute
    `table_name`.
    
    If `use_main_table` is provided, and is set to True, both `table_in`
    and `table_out` will default to `Alignment.table_name`. Values provided
    for `table_in` and `table_out` at calling time will be ignored.
    
    The following cases assume `use_main_table` is not provided or set to 
    False.
    
    If both `table_in` and `table_out` are provided, `table_out` is modified
    so that `table_out = Alignment.table_name + table_out`. If `table_out`
    is not provided, it defaults to `Alignment.table_name`.
    
    If the final `table_out` does not exist in the database, create it. 
    In this case, if `table_in` will default to `Alignment.table_name`. If 
    `table_out` already exists in the database, then `table_in=table_out`.
    
    Parameters
    ----------
    func : function
        Decorated function
    
    Notes
    -----
    In order to fully understand the mechanism behind the setup of the
    database tables, one must first know that when an `Alignment` instance
    is created, it generates a table in the database containing the
    original data from the alignment.
    In TriFusion (GUI), this original table MUST NOT be modified, since users
    may want to execute several methods on the same `Alignment` object. 
    Therefore, when a particular method needs to modify the original alignment,
    a new temporary table is created to store the modified version
    until the end of the execution. If a second modification is requested,
    it will also be necessary to set the input table as the output table
    of the previous alignment modification.
    In the case of TriSeq (CLI), there is no such requirement, 
    so it's much simpler to use the same table  for all modifications. 
    
    This decorator greatly simplifies this process in the same way for 
    all methods of the `Alignment` object that modify the original alignment
    data. To accomplish this, all decorated method must have the keyword
    arguments: `table_in` and `table_out` (and `use_main_table`, optionally).
    
    For methods called within the execution of TriFusion, the idea is simple.
    Since we don't know which methods will be used by the user, all chained
    methods that will create the same output file can be called with 
    `table_in=new_table` and `table_out=new_table`. 
    Note that bot arguments have the same value. Whatever is the first method
    being called, it will face the fact that "new_table" does not yet exist.
    In this case, the decorator will create a "new_table" in the database and 
    reset `table_in` to the original `Alignment.table_name`. This ensures that
    the first method will still be able to fetch the alignment data. In the
    following methods, `table_in` and `table_out` will be used based on the
    original values, ensuring that the alignment data is being fetched
    from the last modification made and that the modification chain is 
    maintained. In this way, the execution of `Alignment` methods can
    be the same, regardless of the order or number of operations requested
    by the user.
    
    In the case of TriSeq, the methods should set `use_main_table=True`.
    This will always set `table_in=table_out=Alignment.table_name`, which
    means that the main database will be used for all methods.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):

        # If use_main_table is set, the table_in will be overwritten by
        # table_out
        if "use_main_table" in kwargs:
            if kwargs["use_main_table"]:
                kwargs["table_out"] = args[0].table_name
                kwargs["table_in"] = args[0].table_name
                return func(*args, **kwargs)

        # Get sqlite database cursor and main table name
        sql_cur = args[0].cur

        # Define sqlite table name based on the main Alignment table name
        # and the provided table_name argument
        if "table_out" in kwargs:
            table_out = args[0].table_name + kwargs["table_out"]
            kwargs["table_out"] = table_out
        else:
            table_out = args[0].table_name
            kwargs["table_out"] = args[0].table_name

        # Add table name to active table names
        if table_out not in args[0].tables:
            args[0].tables.append(table_out)

        # Check if the table already exists, if not create it.
        if not sql_cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND"
                " name='{}'".format(table_out)).fetchall():

            sql_cur.execute("CREATE TABLE [{}]("
                             "txId INT,"
                             "taxon TEXT,"
                             "seq TEXT)".format(table_out))
            # Set the table_in argument (table where the sequences will be
            # fecthed) to the main Alignment table name
            kwargs["table_in"] = args[0].table_name
        else:
            # Since the table_out already exists, sequences will be fetched
            # from this table instead of the main table name
            kwargs["table_in"] = table_out

        return func(*args, **kwargs)

    return wrapper


def setup_intable(func):
    """Decorator handling active database table when fetching data.
    
    This class is mean to decorate methods of the `Alignment` class that
    retrieves alignment data from the database. The requirement is that
    these methods have the positional arguments `table_suffix`, 
    `table_name` and `table`. These are all optional, and only `table_suffix`
    and `table_name` should be used when calling these methods. The values
    of these two will be used to define the value of `table`, so any
    value provided to this argument when calling the decorated method will
    be ignored. In the end, the `table` variable will contain the final
    table name and only this variable will be used to retrieve the alignment
    data.
    
    `table_suffix` is a suffix that is appended to `Alignment.table_name`.
    `table_name` defines a complete table name.
    
    If neither `table_suffix` nor `table_name` are provided, then `table`
    will default to `Alignment.table_name`.
    
    If `table_name` is provided, `table=table_name`. If `table_suffix`
    is provided, `table=Alignment.table_name + table_suffix`.
    
    If both are provided, `table_name` takes precedence over `table_suffix`
    and `table=table_name`.
    
    In any case, we test the existence of the final `table` value in the
    database. If it does not exist, `table` will revert to 
    `Alignment.table_name` to prevent errors.
    
    Parameters
    ----------
    func : function
        Decorated function
    
    Attributes
    ----------
    func : function
        Decorated function
    """

    # functools.wraps(func) allows the correct generation of docstrings
    # by sphinx of the decorated objects.
    @functools.wraps(func)
    def wrapper(*args, **kwargs):

        # Get sqlite database cursor and main table name
        sql_cur = args[0].cur

        if "table_name" not in kwargs and "table_suffix" not in kwargs:
            kwargs["table"] = args[0].table_name
            return func(*args, **kwargs)

        try:
            tb_name = kwargs["table_name"]
        except KeyError:
            tb_name = None

        try:
            tb_suffix = kwargs["table_suffix"]
        except KeyError:
            tb_suffix = ""

        if tb_name:
            table = tb_name
        elif tb_suffix:
            table = args[0].table_name + tb_suffix
        else:
            table = args[0].table_name

        # Check if the specified table exists. If not, revert to
        # self.table_name. When executing Process operations, the first valid
        # operation may be one that only has a "table_in" argument but several
        # other operations may be defined before with a different table suffix.
        # In such case, the table passed to "table_in" was not yet created and
        # populated in those prior methods. Therefore, here we account for
        # those cases by reverting the table to self.table_name if table_in
        # was not yet created.
        if not sql_cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND"
                " name='{}'".format(table)).fetchall():
            table = args[0].table_name

        kwargs["table"] = table

        return func(*args, **kwargs)

    return wrapper


class AlignmentException(Exception):
    """ Generic Alignment object exception. """
    pass


class AlignmentUnequalLength(Exception):
    """ Raised when sequences in alignment have unequal length. """
    pass


class Alignment(Base):
    """ Main interface with single alignment files.
    
    The `Alignment` class is the main interface with single alignment files, 
    providing methods to parse, query, retrieve and modify alignment data.
    `Alignment` instances can be created by providing only a single argument
    that can be the the path to the alignment file, in which case the file
    is parsed and stored in a sqlite database, or the name of a table in the
    sqlite database. 
    
    When an `Alignment` object is instantiated, it first generates the 
    `table_name` based on the `input_alignment` string, filtering all
    characters that are not alpha numeric. Then, it queries the database
    to check is a table already exists with that name. If yes, it is assumed
    that the alignment data is stored in the provided table name. If there
    is no table with that name, it is assumed that `input_alignment` is a 
    path to the alignment file and the regular parsing ensues. An empty
    table is created, the sequence type, format and missing data symbol are
    automatically detected and the alignment is parsed according to the
    detected format.
    
    Parameters
    ----------
    input_alignment : str
        Can be either the path to an alignment file or the name of a table
        in the sqlite database.
    input_format : str
        File format of `input_alignment`. If `input_alignment` is a
        file path, the format will be automatically detect from the file.
        The value provided with this argument overrides the automatic 
        detection's result.
    partitions : `trifusion.process.data.Partitions`
        If provided, it will set the `partitions` attribute. This should
        be used only when `input_alignment` is a database table name.
    locus_length : int
        Sets the length of the current alignment, stored in the `locus_length`
        attribute. This option should only be used when `input_alignment`
        is a database table name. Otherwise, it is automatically set during
        alignment parsing.
    sequence_code : tuple
        Sets the `sequence_code` attribute with the information on
        (<sequence_type>, <missing data symbol>). This option should only be
         used when `input_alignment` is a database table name. Otherwise, 
         it is automatically set during alignment parsing.
    taxa_list : list
        Sets the list attribute `taxa_list` with the names of the taxa
        present in the alignment. This option should only be
        used when `input_alignment` is a database table name. Otherwise,
        it is automatically set during alignment parsing.
    taxa_idx : dict
        Sets the dictionary attribute `taxa_idx` that maps the taxon names
        to their index in the sqlite database table. This option should only
        be used when `input_alignment` is a database table name. Otherwise,
        it is automatically set during alignment parsing.
    sql_cursor : sqlite3.Cursor
        Cursor object of the sqlite database.
    sql_con : sqlite3.Connection
        Connection object of the sqlite database.
    
    Attributes
    ----------
    cur : sqlite3.Cursor
        Cursor object of the sqlite database.
    con : sqlite3.Connection
        Connection object of the sqlite database.
    table_name : str 
        Name of the sqlite database's table storing the sequence
        data.
    tables : list
        Lists the sqlite database's tables created for the `Alignment`
        instance. The "master" table is always present and represents the
        original alignment.
    partitions : `trifusion.process.data.Partitions`
        Stores the partitions and substitution model definition for the
        alignment.
    locus_length : int
        Length of the alignment in base pairs or residues.
    restriction_range : str
        Only used when gaps are coded in a binary matrix. Stores a string
        with the range of the restriction-type data that will encode
        gaps and will only be used when nexus is in the output format.
    e : None or Exception
        Stores any exceptions that occur during the parsing of the 
        alignment file. If remains None unless something wrong happens.
    taxa_list : list
        List with the active taxon names.
    taxa_idx: dict
        Maps the taxon names to their corresponding index in the sqlite 
        database. The index is not retrieve from the position of the taxon
        in `taxa_list` to prevent messing up when taxa are removed from the
        `Alignment` object.
    shelved_taxa : list
        List of ignored taxon names. 
    path : str
        Full path to alignment file.
    sname : str
        Basename of the alignment file without the extension.
    name : str
        Basename of the alignment file with the extension.
    sequence_code : tuple
        Contains information on (<sequence type>, <missing data symbol>),
        e.g. ("Protein", "x").
    interleave_data : bool
        Attribute that is set to True when interleave data has been
        created for the alignment data.
    
    Notes
    -----
    The `Alignment` class was designed to be a ligthweight, fast and
    powerful interface between alignment data and a set of manipulation
    and transformative methods.
    For performance and efficiency purposes, all alignment data is stored 
    in a sqlite database that prevents the entire alignment from being
    loaded into memory. To facilitate the retrieval and iteration over the
    alignment data, several methods (`iter_columns`, 'iter_sequences`, etc)
    are available to handle the interface with the database and retrieve
    only the necessary information.
    """

    def __init__(self, input_alignment, input_format=None, partitions=None,
                 locus_length=None, sequence_code=None, taxa_list=None,
                 taxa_idx=None, sql_cursor=None, sql_con=None):

        self.cur = sql_cursor
        self.con = sql_con


        if isinstance(partitions, Partitions):
            self.partitions = partitions
        else:
            self.partitions = Partitions()
            """
            Initializing a Partitions instance for the current alignment. By
            default, the Partitions instance will assume that the whole Alignment
            object is one single partition. However, if the current alignment is the
            result of a concatenation, or if a partitions file is provided, the
            partitions argument can be used. Partitions can be later changed using
            the set_partitions method. Substitution models objects are associated
            with each partition and they default to None
            """

        if not locus_length:
            self.locus_length = 0
        else:
            self.locus_length = locus_length
            """
            The length of the alignment object. Even if the current alignment
            object is partitioned, this will return the length of the entire
            alignment
            """

        self.restriction_range = None
        """
        This option is only relevant when gaps are coded. This will store a
        string with the range of the restriction-type data that will encode
        gaps and will only be used when nexus is in the output format
        """

        self.e = None
        """
        The `e` attribute will store any exceptions that occur during the
        parsing of the alignment object. It remains None unless something
        wrong happens.
        """

        self.taxa_list = None
        """
        Attribute that will store the taxa names.
        """

        self.shelved_taxa = []
        """
        Attribute that will store shelved taxa. When retrieving taxa from
        the database, this list will be checked and present taxa will
        be ignored
        """

        self.taxa_idx = None
        """
        Attribute that will store the index of the taxa in the sql database.
        This index is stored in a dictionary instead of being retrieved
        from the index position of the taxa_list list because taxa may
        be removed (which would mess up the link between the two indexes)
        """

        self.path = input_alignment
        """
        Attribute with full path to alignment file
        """
        self.sname = basename(splitext(input_alignment)[0])
        """
        Attribute with basename of alignment file without extension
        """
        self.name = basename(input_alignment)
        """
        Attribute with basename of alignment file
        """

        self.sequence_code = sequence_code
        if taxa_list:
            self.taxa_list = taxa_list
        else:
            self.taxa_list = []
        if taxa_idx:
            self.taxa_idx = taxa_idx
        else:
            self.taxa_idx = {}

        self.table_name = "".join([x for x in self.path if x.isalnum()])
        """
        Creates a database table for the current alignment. This will be the
        link attribute between the sqlite database and the remaining methods
        that require information on the sequence data. This also removes
        any non alpha numeric characters the table name might have and ensures
        that it starts with a aphabetic character to avoid a database error
        """

        self.tables = []
        """
        Lists the currently active tables for the Alignment object. The
        'master' table is always present and represents the original
        alignment. Additional tables may be added as needed, and then dropped
        when no longer necessary. When all tables, except the master, need to
        be removed, this attribute can be used to quickly drop all derived
        tables
        """

        self.interleave_data = False
        """
        Attribute that is set to True when interleave data has been 
        created for the alignment data. Buiding the interleave matrix is a bit
        costly, so when it is requested by the user it is built once and
        stored in the database, and then further usages will use that table.
        """

        """
        NOTE ON POSSIBLE DUPLICATE TABLE NAMES: It is not the responsibility
        of the Alignment class to check on duplicate table names. If an
        exiting table name is found in the database, the Alignment class
        assumes that the alignment has already been parsed and inserted into
        the database. Therefore, it will not parse the alignment file again.
        This usually happens when creating concatenated or reverse concatenated
        alignments, where the sequence data is inserted into the database
        before instantiating the Alignment class. IT IS THE RESPONSABILITY
        OF THE AlignmentList CLASS AND OTHER WRAPPERS TO ASSESS THE VALIDITY
        OR NOT OF POTENTIAL DUPLICATE ALIGNMENT FILES.
        """

        # Check if table for current alignment file already exists. If it
        # does not exist, it is assumed that input_alignment is the path to
        # the alignment file
        if not self._table_exists(self.table_name):

            # Get alignment format and code. Sequence code is a tuple of
            # (DNA, N) or (Protein, X)
            finder_content = self.autofinder(input_alignment)
            # Handles the case where the input format is invalid and
            # finder_content is an Exception
            if isinstance(finder_content, Exception) is False:
                self.input_format, self.sequence_code = self.autofinder(
                    input_alignment)

                # Create database table when there are no issues in the
                # recognition of the file format and sequence code
                self._create_table(self.table_name)

                # In case the input format is specified, overwrite the
                # attribute
                if input_format:
                    self.input_format = input_format

                # parsing the alignment
                self.read_alignment()
            else:
                # Setting the sequence code attribute for seq type checking
                # in AlignmentList
                self.sequence_code = None
                self.e = finder_content

        # In case there is a table for the provided input_alignment
        else:
            self.input_format = input_format

    def __iter__(self):
        """Iterator behavior for `Alignment`.
        
        Iterates over taxa and sequence data from the alignment. Taxon names
        in the `shelved_taxa` attribute are ignored. This will always
        retrieve the alignment data from the master database table, 
        `table_name`.
        
        Yields
        ------
        tx : str
            taxon name.
        seq : str
            sequence string.
        """

        for tx, seq in self.cur.execute(
                "SELECT taxon,seq from [{}]".format(
                    self.table_name)):
            if tx not in self.shelved_taxa:
                yield tx, seq

    def _create_table(self, table_name, cur=None):
        """Creates a new table in the database.
        
        Convenience method that creates a new table in the sqlite database.
        It accepts a custom Cursor object, which overrides the `cur`
        attribute.
        
        Parameters
        ----------
        table_name : str
            Name of the table to be created.
        cur : sqlite3.Cursor
            Custom Cursor object used to query the database.
        """

        if not cur:
            cur = self.cur

        cur.execute("CREATE TABLE [{}]("
                    "txId INT,"
                    "taxon TEXT,"
                    "seq TEXT)".format(table_name))

    def _table_exists(self, table_name, cur=None):
        """ Checks if a table exists in the database.
        
        Convenience method that checks if a table exists in the database.
        It accepts a custom Cursor object, which overrides the `cur`
        attribute.
        
        Parameters
        ----------
        table_name : str
            Name of the table.
        cur: sqlite3.Cursor
            Custom Cursor object used to query the database.

        Returns
        -------
        res : list
            List with the results of a query for 'table' type with 
            `table_name` name. Is empty when the table does not exist.
        
        Notes
        -----
        This returns a list that will contain one item if the table exists
        in the database. If it doesn't exist, returns an empty list.
        Therefore, this can be used simply like
        
            `if self._table_exists("my_table"):`
            `     # Do stuff`
        """

        if not cur:
            cur = self.cur

        res = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND"
            " name='{}'".format(table_name)).fetchall()

        return res

    def _set_format(self, input_format):
        """Manually set the input format of the `Alignment` object.
        
        Manually sets the input format associated with the `Alignment` object

        Parameters
        ----------
        input_format : str
            The input format.
        """

        self.input_format = input_format

    @staticmethod
    def _set_pipes(ns=None, pbar=None, total=None, msg=None):
        """Setup of progress indicators for both GUI and CLI task executions.
         
         This handles the setup of the objects responsible for updating
         the progress of task's execution of both TriFusion (GUI) and
         TriSeq (CLI). At the beginning of any given task, these objects
         can be initialized by providing either the Namespace object (`ns`)
         in the case of TriFusion, or the ProgressBar object (`pbar`), in
         the case of TriSeq. Along with one of these objects, the expected
         `total` of the progress should also be provided. The `ns` and
         `pbar` objects are updated at each iteration of a given task,
         and the `total` is used to get a measure of the progress.

         Optionally, a message can be also provided for the Namespace object
         that will be used by TriFusion.

        Parameters
        ----------
        ns : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        pbar : ProgressBar
            A ProgressBar object used to log the progress of TriSeq execution.
        total : int
            Expected total of the task's progress.
        msg : str
            A secondary message that appears in TriFusion's progress dialogs.

        Notes
        -----
        The progress of any given task can be provided by either an
        `Alignment` or `AlignmentList` instance. Generally, the tasks follow
        the progress of the `AlignmentList` instance, unless that instance
        contains only one `Alignment` object. In that case, the progress
        information is piped from the `Alignment` instance. For that reason,
        and for the Namespace (`ns`) object only, this method first checks
        if the `ns.sa` attribute exists. If it does, it means that the parent
        `AlignmentList` instance contains only one `Alignment` object and,
        therefore, the progress information shoud come from here.

        Examples
        --------
        Start a progress counter  for a task that will make 100 iterations:

        self._set_pipes(ns=ns_obj, pbar=pbar_obj, total=100,
                        msg="Some message")
        """

        # Check if the `sa` attribute exists in the Namespace object.
        # If yes, then the logging will be performed here. If not,
        # The progress attributes will not be set for `ns`.
        try:
            sa = ns.sa
        except AttributeError:
            sa = True

        if ns:
            # Listen for kill switch
            if ns.stop:
                raise KillByUser("")
            # Set attributes only if parent AlignmentList contains a single
            # Alignment object
            if sa:
                ns.total = total
                ns.counter = 0
                ns.msg = msg

        if pbar:
            pbar.max_value = total
            # Reset ProgressBar object counter
            pbar.update(0)

    @staticmethod
    def _update_pipes(ns=None, pbar=None, value=None, msg=None):
        """Update progress indicators for both GUI and CLI task executions.

        This method provides a single interface for updating the progress
        objects `ns` or `pbar`, which should have been initialized at the
        beginning of the task with the `_set_pipes` method.

        Parameters
        ----------
        ns : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        pbar : ProgressBar
            A ProgressBar object used to log the progress of TriSeq execution.
        value : int
            Value of the current progress index
        msg : str
            A secondary message that appears in TriFusion's progress dialogs.

        Notes
        -----
        The progress of any given task can be provided by either an
        `Alignment` or `AlignmentList` instance. Generally, the tasks follow
        the progress of the `AlignmentList` instance, unless that instance
        contains only one `Alignment` object. In that case, the progress
        information is piped from the `Alignment` instance. For that reason,
        and for the Namespace (`ns`) object only, this method first checks
        if the `ns.sa` attribute exists. If it does, it means that the parent
        `AlignmentList` instance contains only one `Alignment` object and,
        therefore, the progress information shoud come from here.

        Examples
        --------
        Update the counter in an iteration of 100:

            for i in range(100):
                self._update_pipes(ns=ns_obj, pbar=pbar_obj, value=i,
                                   msg="Some string")
        """

        try:
            sa = ns.sa
        except AttributeError:
            sa = True

        if ns:
            if ns.stop:
                raise KillByUser("")
            if sa:
                ns.counter = value
                ns.msg = msg
        if pbar:
            pbar.update(value)

    @staticmethod
    def _reset_pipes(ns):
        """Reset progress indicators for both GUI and CLI task executions.

        This should be done at the end of any task that initialized the
        progress objects, but it only affects the Namespace object. It
        resets all Namespace attributes to None.

        Parameters
        ----------
        ns : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        """

        try:
            sa = ns.sa
        except AttributeError:
            sa = True

        if ns:
            if ns.stop:
                raise KillByUser("")
            if sa:
                ns.total = ns.counter = ns.msg = ns.sa = None

    @setup_intable
    def iter_columns(self, table_suffix="", table_name=None, table=None):
        """Generator over alignment columns.

        Generator that yields the alignment columns in a list of characters.
        Sequence data is retrieved from a database table specified either by
        the `table_name` or `table_suffix` arguments. `table_name` will always
        take precedence over `table_suffix` if both are provided. If none
        are provided, the default `Alignment.table_name` is used. If the
        table name provided by either `table_name` or `table_suffix` is
        invalid, the default `Alignment.table_name` is also used.

        The `table` variable will be automatically setup in the `SetupInTable`
        decorator according to `table_suffix` and `table_name`, and should
        not be used directly (values provided when calling it are actually
        ignored). The decorator will set this variable

        Parameters
        ----------
        table_suffix : string
            Suffix of the table from where the sequence data is fetched.
            The suffix is appended to `Alignment.table_name`.
        table_name : string
            Name of the table from where the sequence data is fetched.
        table : string
            This argument will be automatically setup by the `SetupInTable`
            decorator. Do not use directly.

        Yields
        ------
        i : list
            List with alignment column characters as elements
            (["A", "A", "C", "A", "A"]).

        See Also
        --------
        SetupInTable

        Notes
        -----
        To prevent the query from loading the entire alignment data into
        memory, two techniques are employed. First, the iteration over the
        query results is lazy, using `itertools.izip`. Second, a `range`
        iteration is performed to ensure that only blocks of 100k alignment
        sites are fetch at any given time. Several tests were performed to
        ensure that this block size provided a good trade-off between
        RAM usage and speed.

        All generators ignore data associated with taxa present in the
        `shelved_taxa` attribute.
        """

        try:
            # Locking mechanism necessary to avoid concurrency issues when
            # accessing the database. This ensures that only one Cursor
            # object is querying the database at any given time
            lock.acquire(True)

            for p in range(0, self.locus_length, 100000):
                for i in itertools.izip(*(x[1] for x in self.cur.execute(
                        "SELECT taxon, substr(seq, {}, 100000)"
                        " FROM [{}]".format(p, table))
                               if x[0] not in self.shelved_taxa)):
                    yield i
        finally:
            lock.release()

    @setup_intable
    def iter_columns_uniq(self, table_suffix="", table_name=None,
                          table=None):
        """ Generator over unique characters of an alignment column.

        Generator that yields unique elements of alignment columns in a list.
        Sequence data is retrieved from a database table specified either by
        the `table_name` or `table_suffix` arguments. `table_name` will always
        take precedence over `table_suffix` if both are provided. If none
        are provided, the default `Alignment.table_name` is used. If the
        table name provided by either `table_name` or `table_suffix` is
        invalid, the default `Alignment.table_name` is also used.

        The `table` variable will be automatically setup in the `SetupInTable`
        decorator according to `table_suffix` and `table_name`, and should
        not be used directly (values provided when calling it are actually
        ignored). The decorator will set this variable

        Parameters
        ----------
        table_suffix : string
            Suffix of the table from where the sequence data is fetched.
            The suffix is appended to `Alignment.table_name`.
        table_name : string
            Name of the table from where the sequence data is fetched.
        table : string
            This argument will be automatically setup by the `SetupInTable`
            decorator. Do not use directly.

        Yields
        ------
        i : list
            List with alignment column characters as elements (["A", "C"]).

        See Also
        --------
        SetupInTable

        Notes
        -----
        To prevent the query from loading the entire alignment data into
        memory, two techniques are employed. First, the iteration over the
        query results is lazy, using `itertools.izip`. Second, a `range`
        iteration is performed to ensure that only blocks of 100k alignment
        sites are fetch at any given time. Several tests were performed to
        ensure that this block size provided a good trade-off between
        RAM usage and speed.

        All generators ignore data associated with taxa present in the
        `shelved_taxa` attribute.
        """

        try:
            # Locking mechanism necessary to avoid concurrency issues when
            # accessing the database. This ensures that only one Cursor
            # object is querying the database at any given time
            lock.acquire(True)

            for p in range(0, self.locus_length, 100000):
                for i in itertools.izip(*(x[1] for x in self.cur.execute(
                        "SELECT taxon, substr(seq, {}, 100000)"
                        " FROM [{}]".format(p, table))
                               if x[0] not in self.shelved_taxa)):
                    yield list(set(i))
        finally:
            lock.release()

    @setup_intable
    def iter_sequences(self, table_suffix="", table_name=None, table=None):
        """ Generator over sequence strings in the alignment.

        Generator for sequence data of the `Alignment` object.
        Sequence data is retrieved from a database table specified either by
        the `table_name` or `table_suffix` arguments. `table_name` will always
        take precedence over `table_suffix` if both are provided. If none
        are provided, the default `Alignment.table_name` is used. If the
        table name provided by either `table_name` or `table_suffix` is
        invalid, the default `Alignment.table_name` is also used.

        Parameters
        ----------
        table_suffix : string
            Suffix of the table from where the sequence data is fetched.
            The suffix is appended to `Alignment.table_name`.
        table_name : string
            Name of the table from where the sequence data is fetched.
        table : string
            This argument will be automatically setup by the `SetupInTable`
            decorator. Do not use directly.

        Yields
        ------
        seq : string
            Sequence string for a given taxon

        See Also
        --------
        SetupInTable

        Notes
        -----
        All generators ignore data associated with taxa present in the
        `shelved_taxa` attribute.
        """

        try:
            # Locking mechanism necessary to avoid concurrency issues when
            # accessing the database. This ensures that only one Cursor
            # object is querying the database at any given time
            lock.acquire(True)
            for tx, seq in self.cur.execute(
                    "SELECT taxon,seq FROM [{}]".format(table)):
                if tx not in self.shelved_taxa:
                    yield seq
        finally:
            lock.release()

    @setup_intable
    def iter_alignment(self, table_suffix="", table_name=None, table=None):
        """Generator for (taxon, sequence) tuples.

        Generator that yields (taxon, sequence) tuples from the database.
        Sequence data is retrieved from a database table specified either by
        the `table_name` or `table_suffix` arguments. `table_name` will always
        take precedence over `table_suffix` if both are provided. If none
        are provided, the default `Alignment.table_name` is used. If the
        table name provided by either `table_name` or `table_suffix` is
        invalid, the default `Alignment.table_name` is also used.

        Parameters
        ----------
        table_suffix : string
            Suffix of the table from where the sequence data is fetched.
            The suffix is append to `Alignment.table_name`.
        table_name : string
            Name of the table from where the sequence data is fetched.
        table : string
            This argument will be automatically setup by the `SetupInTable`
            decorator. Do not use directly.

        Yields
        ------
        tx : string
            Taxon name.
        seq : string
            Sequence string.

        See Also
        --------
        SetupInTable

        Notes
        -----
        All generators ignore data associated with taxa present in the
        `shelved_taxa` attribute.
        """

        try:
            # Locking mechanism necessary to avoid concurrency issues when
            # accessing the database. This ensures that only one Cursor
            # object is querying the database at any given time
            lock.acquire(True)
            for tx, seq in self.cur.execute(
                    "SELECT taxon, seq from [{}]".format(table)):
                if tx not in self.shelved_taxa:
                    yield tx, seq
        finally:
            lock.release()

    @setup_intable
    def get_sequence(self, taxon, table_name=None, table_suffix="",
                       table=None):
        """Returns the sequence string for a given taxon.

        Returns the sequence string of the corresponding `taxon`. If the
        taxon does not exist in the table, raises a KeyError.
        The sequence is retrieved from a database table specified either by
        the `table_name` or `table_suffix` arguments. `table_name` will always
        take precedence over `table_suffix` if both are provided. If none
        are provided, the default `Alignment.table_name` is used. If the
        table name provided by either `table_name` or `table_suffix` is
        invalid, the default `Alignment.table_name` is also used.

        Parameters
        ----------
        taxon : str
            Name of the taxon.
        table_suffix : string
            Suffix of the table from where the sequence data is fetched.
            The suffix is append to `Alignment.table_name`.
        table_name : string
            Name of the table from where the sequence data is fetched.
        table : string
            This argument will be automatically setup by the `SetupInTable`
            decorator. Do not use directly.

        Returns
        -------
        seq : str
            Sequence string.

        Raises
        ------
        KeyError
            If the taxon does not exist in the specified table.

        Notes
        -----
        Ignores data associated with taxa present in the `shelved_taxa`
        attribute.
        """

        try:
            # Locking mechanism necessary to avoid concurrency issues when
            # accessing the database. This ensures that only one Cursor
            # object is querying the database at any given time
            lock.acquire(True)
            if taxon in self.taxa_list and taxon not in self.shelved_taxa:
                seq = self.cur.execute(
                    "SELECT seq FROM [{}] WHERE txId=?".format(table),
                    (self.taxa_idx[taxon],)).fetchone()[0]
                return seq

            else:
                raise KeyError
        finally:
            lock.release()

    def remove_alignment(self):
        """ Removes alignment data from the database. """

        # Drop main alignment
        self.cur.execute("DROP TABLE [{}]".format(self.table_name))

        # If additional alignments were created, drop those tables as well
        for table in self.tables:
            try:
                self.cur.execute("DROP TABLE [{}]".format(table))
            except sqlite3.OperationalError:
                pass

    def shelve_taxa(self, lst):
        """Shelves taxa from `Alignment` methods.

        The taxa provided in the `lst` argument will be ignored in
        subsequent operations. In practice, taxa from the `taxa_list`
        attribute that are present in `lst` will be moved to the
        `shelved_taxa` attribute and ignored in all methods of the class.
        To revert all taxa to the `taxa_list` attribute, simply call this
        method with an empty list.

        Parameters
        ----------
        lst : list
            List with taxa names that should be ignored.
        """

        self.shelved_taxa = lst
        self.taxa_list = [x for x in self.taxa_list
                          if x not in self.shelved_taxa]

    def _read_interleave_phylip(self, ntaxa):
        """ Alignment parser for interleave phylip format.

        Parses an interleave phylip alignment file and stores taxa and
        sequence data in the database. This method is only called from the
        `_read_phylip` method, when the regular phylip parser detects that the
        file is in interleave format.

        Parameters
        ----------
        ntaxa : int
            Number of taxa contained in the alignment file.

        Returns
        -------
        size_list : list
            List of sequence size (int) for each taxon in the alignment file.
            Used to check size consistency at the end of the alignment parsing
            in `read_alignment`.

        See Also
        --------
        _read_phylip

        Notes
        -----
        The phylip interleave format splits the alignment into blocks of
        a certain lenght (usually 90 characters) separated by blank lines.
        This means that to gather the complete sequence of a given taxon,
        the parser has to read the entire file. To prevent the entire
        alignment to be loaded into memory, we actually iterate over a range
        determined by the number of taxa in the alignment. In each iteration,
        we open a file handle and we retrieve only the sequence of the
        taxon for a particular taxon. This ensures that only sequence data for
        a single taxon is store in memory at any given time. This also means
        that the alignment file has to be read N times, where N = number of
        taxa. However, since the vast majority of lines are actually skipped
        in each iteration, the decrease in speed is marginal, while the
        gains in memory efficient are much larger.
        """

        size_list = []

        for i in xrange(ntaxa):

            # Variable that will store the sequence for the current taxon.
            sequence = []
            # Index identifier of the current taxon
            idx = 0
            # When True, it means that the alignment line has the taxon
            # and sequence. When False, it means that the line contains
            # only sequence.
            taxa_gather = True

            fh = open(self.path)

            # Skip header an any potential blank lines
            header = ""
            while not header:
                header = next(fh)

            for line in fh:

                # At blank lines, reset the idx and set taxa_gather to False.
                # All lines from this point on will only contain sequence.
                if not line.strip():
                    idx = 0
                    taxa_gather = False
                else:
                    # If the idx of the alignment block matches the taxon
                    # from the current iteration, add to sequence variable
                    if idx == i:
                        # Remove the taxon from the gathering
                        if taxa_gather:
                            sequence.append(
                                "".join(line.strip().lower().split()[1:]))
                        else:
                            sequence.append(
                                "".join(line.strip().lower().split()))
                    idx += 1

            seq = "".join(sequence)
            taxa = self.taxa_list[i]

            size_list.append(len(seq))

            # Insert data into database
            self.cur.execute("INSERT INTO [{}] VALUES (?, ?, ?)".format(
                self.table_name), (i, taxa, seq))

            fh.close()

        return size_list

    def _read_interleave_nexus(self, ntaxa):
        """ Alignment parser for interleave nexus format.

        Parses an interleave nexus alignment file and stores taxa and
        sequence data in the database.This method is only called from the
        `_read_nexus` method, when the regular nexus parser detects that the
        file is in interleave format.

        Parameters
        ----------
        ntaxa : int
            Number of taxa contained in the alignment file.

        Returns
        -------
        size_list : list
            List of sequence size (int) for each taxon in the alignment file.
            Used to check size consistency at the end of the alignment parsing
            in `read_alignment`.

        See Also
        --------
        _read_nexus

        Notes
        -----
        The nexus interleave format splits the alignment into blocks of
        a certain lenght (usually 90 characters) separated by blank lines.
        This means that to gather the complete sequence of a given taxon,
        the parser has to read the entire file. To prevent the entire
        alignment to be loaded into memory, we actually iterate over a range
        determined by the number of taxa in the alignment. In each iteration,
        we open a file handle and we retrieve only the sequence of the
        taxon for a particular taxon. This ensures that only sequence data for
        a single taxon is store in memory at any given time. This also means
        that the alignment file has to be read N times, where N = number of
        taxa. However, since the vast majority of lines are actually skipped
        in each iteration, the decrease in speed is marginal, while the
        gains in memory efficient are much larger.
        """

        size_list = []

        for i in xrange(ntaxa):

            # Variable that will store the sequence for the current taxon.
            sequence = []
            # counter used to skip the Nexus header and footer
            counter = 0
            # Index identifier of the current taxon
            idx = 0
            taxa = None

            fh = open(self.path)

            for line in fh:

                if line.strip().lower() == "matrix" and counter == 0:
                    counter = 1

                elif line.strip() == ";" and counter == 1:
                    counter = 2

                elif counter == 1:

                    if not line.strip():
                        idx = 0
                    else:
                        if idx == i:
                            if not taxa:
                                taxa = line.strip().split()[0].replace(" ",
                                                                       "")
                                taxa = self.rm_illegal(taxa)

                            sequence.append(
                                "".join(line.strip().lower().split()[1:]))

                        idx += 1

            self.taxa_list.append(taxa)
            self.taxa_idx[taxa] = i

            seq = "".join(sequence)
            if not self.locus_length:
                self.locus_length = len(seq)

            size_list.append(len(seq))

            self.cur.execute("INSERT INTO [{}] VALUES (?, ?, ?)".format(
                self.table_name), (i, taxa, seq))

            fh.close()

    def _eval_missing_symbol(self, sequence):
        """Evaluates missing data symbol from sequence string.

        This method is performed when sequence data is being parsed from the
        alignment and only executes when the missing data symbol stored in
        `Alignment.sequence_code` is not defined. It attempts to count
        the regular characters used to denote missing data. First, tries to
        find "?" characters, which are set as the symbol if they exist.
        Then, it finds "n" characters and if sequence type is set to DNA,
        this character is set as the symbol.
        Finally, it finds "x" characters and if sequence type is set to
        Protein, this character is set as the symbol.

        Parameters
        ----------
        sequence : str
            Sequence string

        """

        if not self.sequence_code[1]:

            if sequence.count("?"):
                self.sequence_code[1] = "?"

            elif sequence.count("n") and self.sequence_code[0] == "DNA":
                self.sequence_code[1] = "n"

            elif sequence.count("x") and self.sequence_code[0] == "Protein":
                self.sequence_code[1] = "x"

    def _read_phylip(self):
        """Alignment parser for phylip format.

        Parses a phylip alignment file and stored taxa and sequence data in
        the database.

        Returns
        -------
        size_list : list
            List of sequence size (int) for each taxon in the alignment file.
            Used to check size consistency at the end of the alignment parsing
            in `read_alignment`.

        See Also
        --------
        read_alignment
        """

        fh = open(self.path)

        # Variable storing the lenght of each sequence
        size_list = []

        # Get the number of taxa and sequence length from the file header
        header = fh.readline().split()
        self.locus_length = int(header[1])
        self.partitions.set_length(self.locus_length)
        taxa_num = int(header[0])

        # These three following attributes allow the support for
        # interleave phylip files
        # Flags whether the current line should be parsed for taxon name
        taxa_gather = True
        # Counter that makes the correspondence between the current line
        # and the appropriate taxon
        c = 0

        for line in fh:

            # Ignore empty lines
            if line.strip() == "":
                continue

            # The counter is reset when surpassing the number of
            # expected taxa.
            if c + 1 > taxa_num:
                c = 0
                taxa_gather = False

            try:

                # Here, assume that all lines with taxon names have
                # already been processed, since he taxa_pos variable
                # already has the same number as the expected taxa in the
                #  phylip header
                if not taxa_gather:

                    # Oh boy, this seems like an interleave phylip file.
                    # Redirect parsing to appropriate method
                    self.cur.execute("DELETE FROM [{}]".format(
                        self.table_name))
                    size_list = self._read_interleave_phylip(taxa_num)
                    break

                # To support interleave phylip, while the taxa_pos
                # variable has not reached the expected number of taxa
                # provided in header[0], treat the line as the first
                # lines of the phylip where the first field is the taxon
                # name
                elif taxa_gather:

                    taxa = line.split()[0].replace(" ", "")
                    taxa = self.rm_illegal(taxa)

                    try:
                        # Joint multiple batches of sequence
                        # Remove any possible whitespace by splitting
                        # according to whitespace. This also ensures that
                        # sequence is always a list when added to
                        # sequence_data
                        sequence = line.strip().lower().split()[1:]
                    except IndexError:
                        sequence = [""]

                    self.taxa_list.append(taxa)
                    self.taxa_idx[taxa] = c

                    seq = "".join(sequence)

                    # Evaluate missing data symbol if undefined
                    self._eval_missing_symbol(seq)

                    self.cur.execute("INSERT INTO [{}] VALUES"
                                     "(?, ?, ?)".format(
                        self.table_name), (c, taxa, seq))

                    size_list.append(len(seq))

                    # Add counter for interleave processing
                    c += 1

                    # sequence_data.append((taxa, "".join(sequence)))

            except IndexError:
                pass

        # Updating partitions object
        self.partitions.add_partition(self.name, self.locus_length,
                                      file_name=self.path)
        
        fh.close()

        return size_list

    def _read_fasta(self):
        """Alignment parser for fasta format.

        Parses a fasta alignment file and stored taxa and sequence data in
        the database.

        Returns
        -------
        size_list : list
            List of sequence size (int) for each taxon in the alignment file.
            Used to check size consistency at the end of the alignment parsing
            in `read_alignment`.

        See Also
        --------
        read_alignment
        """

        fh = open(self.path)

        # Variable storing the lenght of each sequence
        size_list = []

        sequence = []
        idx = 0
        for line in fh:
            if line.strip().startswith(">"):

                if sequence:
                    seq = "".join(sequence)

                    # Evaluate missing data symbol if undefined
                    self._eval_missing_symbol(seq)

                    self.cur.execute("INSERT INTO [{}] VALUES"
                                     " (?, ?, ?)".format(
                        self.table_name), (idx, taxa, seq))

                    self.taxa_list.append(taxa)
                    self.taxa_idx[taxa] = idx

                    if not self.locus_length:
                        self.locus_length = len(seq)

                    size_list.append(len(seq))

                    # sequence_data.append((taxa, "".join(sequence)))
                    sequence = []
                    idx += 1

                taxa = line[1:].strip()
                taxa = self.rm_illegal(taxa)

            elif line.strip() != "" and taxa:
                sequence.append(line.strip().lower().
                                replace(" ", "").replace("*", ""))

        if sequence:
            seq = "".join(sequence)

            # Evaluate missing data symbol if undefined
            self._eval_missing_symbol(seq)

            self.cur.execute("INSERT INTO [{}] VALUES"
                             " (?, ?, ?)".format(
                self.table_name), (idx, taxa, seq))

            self.taxa_list.append(taxa)
            self.taxa_idx[taxa] = idx

            if not self.locus_length:
                self.locus_length = len(seq)

        size_list.append(len(seq))

        self.partitions.set_length(self.locus_length)

        # Updating partitions object
        self.partitions.add_partition(self.name, self.locus_length,
                                      file_name=self.path)

        fh.close()

        return size_list

    def _read_loci(self):
        """Alignment parser for pyRAD and ipyrad loci format.

        Returns
        -------
        size_list : list
            List of sequence size (int) for each taxon in the alignment file.
            Used to check size consistency at the end of the alignment parsing
            in `read_alignment`.

        See Also
        --------
        read_alignment
        """

        fh = open(self.path)

        # Variable storing the lenght of each sequence
        size_list = []

        taxa_list = self.get_loci_taxa(self.path)

        # Create empty dict
        sequence_data = dict([(tx, []) for tx in taxa_list])

        # Add a counter to name each locus
        locus_c = 1
        # This variable is used in comparison with taxa_list to check which
        # taxa are missing from the current partition. Missing taxa
        # are filled with missing data.
        present_taxa = []

        for line in fh:
            if not line.strip().startswith("//") and line.strip() != "":
                fields = line.strip().split()
                taxon = fields[0].lstrip(">")
                present_taxa.append(taxon)
                sequence_data[taxon].append(fields[1].lower())

            elif line.strip().startswith("//"):

                locus_len = len(fields[1])
                self.locus_length += locus_len
                size_list.append(locus_len)

                # Adding missing data
                for tx in taxa_list:
                    if tx not in present_taxa:
                        sequence_data[tx].append(self.sequence_code[1] *
                                                 locus_len)

                present_taxa = []

                self.partitions.add_partition("locus_{}".format(locus_c),
                                              locus_len,
                                              file_name=self.path)
                locus_c += 1

        sequence_data = [(p, tx, "".join(seq)) for p, (tx, seq) in
                         enumerate(sorted(sequence_data.items()))]

        self.cur.executemany("INSERT INTO [{}] VALUES (?, ?, ?)".format(
            self.table_name), sequence_data)

        self.partitions.set_length(self.locus_length)

        fh.close()

        return size_list

    def _read_nexus(self):
        """Alignment parser for nexus format.

        Parses a nexus alignment file and stored taxa and sequence data in
        the database.

        Returns
        -------
        size_list : list
            List of sequence size (int) for each taxon in the alignment file.
            Used to check size consistency at the end of the alignment parsing
            in `read_alignment`.

        See Also
        --------
        read_alignment
        """

        fh = open(self.path)

        # Variable storing the lenght of each sequence
        size_list = []

        counter = 0
        idx = 0
        ntaxa = None
        interleave = None
        for line in fh:

            # Fetch the number of taxa from nexus header. This will be
            # necessary for efficient interleave parsing
            if not ntaxa:
                try:
                    ntaxa = int(re.search(r".*ntax=(.+?) ",
                                          line).group(1))
                except ValueError:
                    self.e = InputError("Could not recognize number"
                                        " of taxa from nexus header")
                    return
                except AttributeError:
                    pass

            # Fetch the number of sites from nexus header. This will be
            # necessary for efficient interleave parsing
            if not self.locus_length:
                try:
                    self.locus_length = int(
                        re.search(r".*nchar=(.+?)[ ,;]",
                                  line).group(1))
                except ValueError:
                    self.e = InputError("Could not recognize number"
                                        " of sites from nexus header")
                    return
                except AttributeError:
                    pass

            # Fetch the interleave value from the  nexus header. This
            # will be necessary for efficient interleave parsing
            if interleave == None:
                try:
                    interleave = re.search(r"interleave=(.+?) ",
                                           line).group(1).lower()
                    interleave = True if interleave.strip() == "yes" \
                        else False
                except AttributeError:
                    pass

            # Set the counter to beggin data parsing
            if line.strip().lower() == "matrix" and counter == 0:
                counter = 1

            # Stop sequence parser here
            elif line.strip() == ";" and counter == 1:
                counter = 2

                # self.locus_length = len(sequence_data[0][2])
                self.partitions.set_length(self.locus_length)

            # Start parsing here
            elif counter == 1:

                # If neither ntaxa or interleave are defined, return
                # with an error in the 'e' attribute
                if not ntaxa or interleave == None:
                    self.e = InputError("Could not recognize ntax or"
                                        " interleave parameters from"
                                        " nexus header")
                    return

                if interleave == False:

                    # In interleave, this marks the change in matrix blocks
                    if not line.strip():
                        continue

                    # Get taxon name
                    taxa = line.strip().split()[0].replace(" ", "")
                    taxa = self.rm_illegal(taxa)

                    # Update taxa_list and taxa_idx attributes
                    self.taxa_list.append(taxa)
                    self.taxa_idx[taxa] = idx

                    # Get sequence string
                    seq = "".join(line.strip().split()[1].split())

                    # Evaluate missing data symbol if undefined
                    self._eval_missing_symbol(seq)

                    size_list.append(len(seq))

                    # Add sequence to sqlite database
                    self.cur.execute(
                        "INSERT INTO [{}] VALUES (?, ?, ?)".format(
                            self.table_name), (idx, taxa, seq))

                    idx += 1

                else:
                    self._read_interleave_nexus(self.path, ntaxa)
                    counter = 2

            # If partitions are specified using the charset command, this
            # section will parse the partitions
            elif line.strip().startswith("charset"):
                self.partitions.read_from_nexus_string(line,
                                                       file_name=self.path)

            # If substitution models are specified using the lset or prset
            # commands, this will parse the model parameters
            if ("lset" in line.lower() or "prset" in line.lower()) and \
                            counter == 2:
                self.partitions.parse_nexus_model(line)

        # If no partitions have been added during the parsing of the nexus
        # file, set a single partition
        if self.partitions.partitions == OrderedDict():
            self.partitions.add_partition(self.name, self.locus_length,
                                          file_name=self.path)

        fh.close()

        return size_list

    def _read_stockholm(self):
        """Alignment parser for stockholm format.

        Parses a stockholm alignment file and stored taxa and sequence data in
        the database.

        Returns
        -------
        size_list : list
            List of sequence size (int) for each taxon in the alignment file.
            Used to check size consistency at the end of the alignment parsing
            in `read_alignment`.

        See Also
        --------
        read_alignment
        """

        fh = open(self.path)

        # Variable storing the lenght of each sequence
        size_list = []

        sequence_data = []
        # Skip first header line
        next(fh)

        for line in fh:
            # Skip header and comments
            if line.startswith("#"):
                pass
            # End of file
            elif line.strip() == "//":
                break
            # Parse sequence data
            elif line.strip() != "":

                taxa = line.split()[0].split("/")[0]
                taxa = self.rm_illegal(taxa)

                try:
                    sequence = line.split()[1].strip().lower()
                    # Evaluate missing data symbol if undefined
                    self._eval_missing_symbol(sequence)
                except IndexError:
                    sequence = ""

                size_list.append(len(sequence))

                sequence_data.append((taxa, sequence))

        sequence_data = [(p, x, y) for p, (x, y) in
                         enumerate(sorted(sequence_data))]

        self.cur.executemany("INSERT INTO [{}] VALUES (?, ?, ?)".format(
            self.table_name), sequence_data)

        self.locus_length = len(sequence_data[0][1])
        self.partitions.set_length(self.locus_length)

        # Updating partitions object
        self.partitions.add_partition(self.name, self.locus_length,
                                      file_name=self.path)

        fh.close()

        return size_list

    def read_alignment(self, size_check=True):
        """Main alignment parser method.

        This is the main alignment parsing method that is called when the
        `Alignment` object is instantiated with a file path as the argument.
        Given the alignment format automatically detected in `__init__`,
        it calls the specific method that parses that alignment format.
        After the execution of this method, all attributes of the class will
        be set and the full range of methods can be applied.
        
        Parameters
        ----------
        size_check : bool
            If True, perform a check for sequence size consistency across
            taxa (default is True).
        """

        parsing_methods = {
            "phylip": self._read_phylip,
            "fasta": self._read_fasta,
            "loci": self._read_loci,
            "nexus": self._read_nexus,
            "stockholm": self._read_stockholm
        }

        size_list = parsing_methods[self.input_format]()

        # If the missing data symbol could not be evaluated during alignment
        # parsing, set the defaults
        default_missing = {"DNA": "n", "Protein": "x"}
        if not self.sequence_code[1]:
            self.sequence_code[1] = default_missing[self.sequence_code[0]]

        # Checks the size consistency of the alignment
        if size_check is True:
            if len(set(size_list)) > 1:
                self.e = AlignmentUnequalLength()
                return

        if len(self.taxa_list) != len(set(self.taxa_list)):
            duplicate_taxa = self.duplicate_taxa(self.taxa_list)
            self.e = DuplicateTaxa("The following taxa were duplicated in"
                                   " the alignment: {}".format(
                "; ".join(duplicate_taxa)))

    def remove_taxa(self, taxa_list_file, mode="remove"):
        """ Removes taxa from the `Alignment` object.

        Removes taxa based on the  `taxa_list_file` argument from the
        `Alignment` object.

        This method supports a list or path to CSV file as the
        `taxa_list_file` argument. In case of CSV path, it parses the CSV
        file into a list. The CSV file should be a simple text file containing
        taxon names in each line.

        This method also supports two removal modes.
            - remove: removes the specified taxa
            - inverse: removes all but the specified taxa

        Parameters
        ----------
        taxa_list_file : list or string
            List containing taxa names or string with path to a CSV file
            containig the taxa names.
        mode : {"remove", "inverse"}
            The removal mode (default is remove).
        """

        def remove(list_taxa):
            for tx in list_taxa:
                self.cur.execute(
                    "DELETE FROM [{}] WHERE txId=?".format(self.table_name),
                    (self.taxa_idx[tx],))
                self.taxa_list.remove(tx)
                del self.taxa_idx[tx]

        def inverse(list_taxa):
            for tx in list(self.taxa_list):
                if tx not in list_taxa:
                    self.cur.execute(
                        "DELETE FROM [{}] WHERE txId=?".format(
                            self.table_name),
                        (self.taxa_idx[tx],))
                    self.taxa_list.remove(tx)
                    del self.taxa_idx[tx]

        # Checking if taxa_list is an input csv file:
        try:
            file_handle = open(taxa_list_file[0])
            taxa_list = self.read_basic_csv(file_handle)

        # If not, then the method's argument is already the final list
        except (IOError, IndexError):
            taxa_list = taxa_list_file

        # Filter taxa_list for the taxa that are actually present in this
        # Alignment object
        taxa_list = [x for x in taxa_list if x in self.taxa_list]

        if mode == "remove":
            remove(taxa_list)
        if mode == "inverse":
            inverse(taxa_list)

    def change_taxon_name(self, old_name, new_name):
        """Changes the name of a particular taxon.

        Parameters
        ----------
        old_name : str
            Original taxon name.
        new_name : str
            New taxon name.
        """

        # Change in taxa_list
        if old_name in self.taxa_list:
            self.taxa_list[self.taxa_list.index(old_name)] = new_name
            # Change in the database
            self.cur.execute("UPDATE [{}] SET taxon=? WHERE txId=?".format(
                self.table_name), (new_name, self.taxa_idx[old_name]))
            # Change in taxa_index
            self.taxa_idx[new_name] = self.taxa_idx[old_name]
            del self.taxa_idx[old_name]

    @setup_database
    def collapse(self, write_haplotypes=True, haplotypes_file=None,
                 dest=".", conversion_suffix="", haplotype_name="Hap",
                 table_in=None, table_out="collapsed", use_main_table=False,
                 ns=None, pbar=None):
        """Collapses equal sequences into unique haplotypes.

        Collapses equal sequences into unique haplotypes. This method fetches
        the sequences for the current alignment and creates a new database
        table with the collapsed haplotypes.

        Parameters
        ----------
        write_haplotypes : bool
            If True, a file mapping the taxa names name to their respective
            haplotype name will be generated (default is True).
        haplotypes_file : string
            Name of the file mapping taxa names to their respective haplotype.
            Only used when `write_haplotypes` is True. If it not provided and
            `write_haplotypes` is True, the file name will be determined
            from `Alignment.name`.
        dest : string
            Path of directory where `haplotypes_file` will be generated
            (default is ".").
        conversion_suffix : string
            Suffix appended to the haplotypes file. Only used when
            `write_haplotypes` is True and `haplotypes_file` is None.
        haplotype_name : string
            Prefix of the haplotype string. The final haplotype name will be
            `haplotype_name` + <int> (default is "Hap").
        table_in : string
            Name of database table containing the alignment data that is
            used for this operation.
        table_out : string
            Name of database table where the final alignment will be inserted
            (default is "collapsed").
        use_main_table : bool
            If True, both `table_in` and `table_out` are ignore and the main
            table `Alignment.table_name` is used as the input and output
            table (default is False).
        ns : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        pbar : ProgressBar
            A ProgressBar object used to log the progress of TriSeq execution.

        See Also
        --------
        SetupDatabase

        Notes
        -----
        In order to collapse an alignment, all sequence would need to be
        compared and somehow stored until the last sequence is processed.
        This creates the immediate issue that, if all sequences are different,
        the entire alignment could have to be stored in memory. To increase
        memory efficiency and increase performance, sequences are converted
        into hashes, and these hashes are compared among each other instead
        of the actual sequences. This means that instead of comparing
        potentially very long string and maintaining them in memory, only a
        small hash string is maintained and compared. In this way, only a
        fraction of the information is stored in memory and the comparisons
        are much faster.
        """

        self._set_pipes(ns, pbar, len(self.taxa_list))

        # Create temporary table for storing unique sequences
        self._create_table(".collapsed")

        # Create a new cursor to edit the database while iterating over
        # table_in
        col_cur = self.con.cursor()

        # Dict that will store the hashes of each sequence. It will be used to
        # check if the current sequence has already been processed. The value
        # will be the haplotype
        hash_list = {}

        # hap_dic will store the haplotype name as key and a list of the
        # taxa with the same sequence as a list value
        hap_dic = {}
        hap_counter = 0

        for p, (taxa, seq) in enumerate(self.iter_alignment(
                table_name=table_in)):

            self._update_pipes(ns, pbar, p + 1,
                               "Collapsing taxon {}".format(taxa))

            cur_hash = hash(seq)

            # If the current hash is unique, add it to the database
            if cur_hash not in hash_list:

                # Create name for new haplotype and update other haplotype
                # related variables
                haplotype = "{}_{}".format(haplotype_name, hap_counter + 1)
                hash_list[cur_hash] = haplotype
                hap_dic[haplotype] = [taxa]

                # Add data to table
                col_cur.execute("INSERT INTO [.collapsed] "
                                "VALUES (?, ?, ?)", (hap_counter,
                                                     unicode(haplotype),
                                                     seq))

                hap_counter += 1

            else:

                hap_dic[hash_list[cur_hash]].append(taxa)

        # The collapse operation is special in the sense that the former taxon
        # names are no longer valid. Therefore, we drop the previous table and
        # populate a new one with the collapsed data
        if self._table_exists(table_out):
            self.cur.execute("DROP TABLE [{}];".format(table_out))

        # Replace table_out with collapsed table
        self.cur.execute("ALTER TABLE [.collapsed] RENAME TO [{}]".format(
            table_out))

        if write_haplotypes is True:
            # If no output file for the haplotype correspondence is provided,
            # use the input alignment name as reference
            if not haplotypes_file:
                haplotypes_file = self.name.split(".")[0] + conversion_suffix
            self.write_loci_correspondence(hap_dic, haplotypes_file,
                                           dest)

        self._reset_pipes(ns)

    @staticmethod
    def write_loci_correspondence(hap_dict, output_file, dest="./"):
        """Writes the file mapping taxa to unique haplotypes for `collapse`.

        Parameters
        ----------
        hap_dict : dict
            Dictionary mapping the haplotype (key) to a list of taxa (value)
            sharing the same haplotype.
        output_file : str
            Name of the haplotype correspondence file
        dest : str
            Path to directory where the `output_file` will be written.
        """

        output_handle = open(join(dest, output_file + ".haplotypes"), "w")

        for haplotype, taxa_list in sorted(hap_dict.items()):
            output_handle.write("%s: %s\n" % (haplotype, "; ".join(taxa_list)))

        output_handle.close()

    @setup_database
    def consensus(self, consensus_type, table_in=None, table_out="consensus",
                  use_main_table=False, ns=None, pbar=None):
        """Creates a consensus sequence from the alignment.

        Converts the current `Alignment` alignment data into a single
        consensus sequence. The `consensus_type` argument determines how
        variation in the original alignment is handled for the generation
        of the consensus sequence.

        The options for handling sequence variation are:

            - IUPAC: Converts variable sites according to the corresponding
              IUPAC symbols (DNA sequence type only)

            - Soft mask: Converts variable sites into missing data

            - Remove: Removes variable sites

            - First sequence: Uses the first sequence in the dictionary

        Parameters
        ----------
        consensus_type : {"IUPAC", "Soft mask", "Remove", "First sequence"}
            Type of variation handling. See summary above.
        table_in : string
            Name of database table containing the alignment data that is
            used for this operation.
        table_out : string
            Name of database table where the final alignment will be inserted
            (default is "consensus").
        use_main_table : bool
            If True, both `table_in` and `table_out` are ignore and the main
            table `Alignment.table_name` is used as the input and output
            table (default is False).
        ns : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        pbar : ProgressBar
            A ProgressBar object used to log the progress of TriSeq execution.

        See Also
        --------
        SetupDatabase
        """

        self._set_pipes(ns, pbar, self.locus_length)

        # Empty consensus sequence
        consensus_seq = []

        # If sequence type is first sequence
        if consensus_type == "First sequence":
            # Grab first available sequence
            consensus_seq = ""
            idx = 0
            while not consensus_seq:
                try:
                    consensus_seq = [self.cur.execute(
                        "SELECT seq from [{}] WHERE txId={}".format(
                            table_in, idx)).fetchone()[0]]
                except TypeError:
                    idx += 1

        for p, column in enumerate(self.iter_columns(table_name=table_in)):

            self._update_pipes(ns, pbar, value=p + 1)

            column = list(set(column))

            # If invariable, include in consensus and skip. Increases speed
            # when there are no missing or gap characters
            if len(column) == 1:
                consensus_seq.append(column[0])
                continue

            # Remove missing data and gaps
            column = sorted([x for x in column if
                             x != self.sequence_code[1] and x != "-"])

            # In case there is only missing/gap characters
            if not column:
                consensus_seq.append(self.sequence_code[1])
                continue

            # Check for invariable without missing data
            if len(column) == 1:
                consensus_seq.append(column[0])
                continue

            if consensus_type == "IUPAC":
                # Convert variation into IUPAC code
                try:
                    consensus_seq.append(iupac["".join(column)])
                except KeyError:
                    iupac_code = sorted(set(list(itertools.chain.from_iterable(
                        [x if x in dna_chars else iupac_rev[x]
                         for x in column]))))
                    consensus_seq.append(iupac["".join(iupac_code)])
                continue

            elif consensus_type == "Soft mask":
                consensus_seq.append(self.sequence_code[1])
                continue

            elif consensus_type == "Remove":
                continue

        # The consensus operation is special in the sense that the former taxon
        # names are no longer valid. Therefore, we drop the previous table and
        # populate a new one with the consensus data
        if self._table_exists(table_out):
            self.cur.execute("DELETE FROM [{}];".format(table_out))

        # Insert into database
        self.cur.execute("INSERT INTO [{}] VALUES(?, ?, ?)".format(
            table_out), (0, "consensus", "".join(consensus_seq)))

        self.taxa_list = ["consensus"]
        self.taxa_idx["consensus"] = 0

        if ns:
            if ns.stop:
                raise KillByUser("")
            if ns.sa:
                ns.counter = ns.total = None

    def _check_partitions(self, partition_obj):
        """Consistency check of the `partition_obj` for `Alignment` object.

        Performs checks to ensure that a `partition_obj` is consistent
        with the alignment data of the `Alignment` object.

        Parameters
        ----------
        partition_obj : trifusion.process.data.Partitions
            Partitions object.
        """

        # Checks if total lenght of partitions matches the lenght of the
        # current alignment

        if partition_obj.counter != self.locus_length:
            return process.data.InvalidPartitionFile("Partitions in partition"
                   " file are inconsistent with current alignment. Last "
                   "partition range: {}; Alignmet length: {}".format(
                partition_obj.counter, self.locus_length
            ))

    def set_partitions(self, partitions):
        """Updates the `partition` attribute.

        Parameters
        ----------
        partitions : trifusion.process.data.Partitions
            Partitions object.
        """

        # Checks partition's consistency
        er = self._check_partitions(partitions)

        if isinstance(er, process.data.InvalidPartitionFile):
            return er
        else:
            self.partitions = partitions

    def reverse_concatenate(self, table_in="", db_con=None, ns=None,
                            pbar=None):
        """Reverses the alignment according to the `partitions` attribute.

        This method splits the current alignment according to the
        partitions set in the `partitions` attribute. Returns an
        `AlignmentList` object where each partition is an `Alignment` object.

        Parameters
        ----------
        table_in : string
            Name of database table containing the alignment data that is
            used for this operation.
        db_con : sqlite3.Connection
            Database connection object that will be provided to each new
            `Alignment` object.
        ns : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        pbar : ProgressBar
            A ProgressBar object used to log the progress of TriSeq execution.
        """

        def add_alignment(part_name, part_len, taxa_list, taxa_idx):

            # Create Alignment object
            current_aln = Alignment(part_name, input_format=self.input_format,
                                    sql_cursor=self.cur,
                                    locus_length=part_len,
                                    sequence_code=self.sequence_code,
                                    taxa_list=taxa_list,
                                    taxa_idx=taxa_idx)

            alns.append(current_aln)

        taxa_list_master = defaultdict(list)
        taxa_idx_master = defaultdict(dict)

        self._set_pipes(ns, pbar, total=len(self.taxa_list))

        rev_cur = self.con.cursor()

        for p, taxon in enumerate(self.taxa_list):

            seq = self.get_sequence(taxon, table_name=table_in)

            self._update_pipes(ns, pbar, value=p + 1)

            for name, part_range in self.partitions:

                name = "".join([x for x in name.split(".")[0] if x.isalnum()])

                if part_range[1]:

                    for i in range(3):

                        part_seq = seq[part_range[0][0]:
                                       part_range[0][1] + 1][i::3]

                        if part_seq.replace(self.sequence_code[1], "") == "":
                            continue

                        cname = name + str(i)

                        taxa_list_master[cname].append(taxon)
                        taxa_idx_master[cname][taxon] = p

                        if not self._table_exists(cname, cur=rev_cur):
                            self._create_table(cname, cur=rev_cur)

                        rev_cur.execute(
                            "INSERT INTO [{}] VALUES"
                            "(?, ?, ?)".format(cname), (p, taxon, part_seq))

                else:

                    part_seq = seq[part_range[0][0]:
                                   part_range[0][1] + 1]

                    if part_seq.replace(self.sequence_code[1], "") == "":
                        continue

                    taxa_list_master[name].append(taxon)
                    taxa_idx_master[name][taxon] = p

                    if not self._table_exists(name, cur=rev_cur):
                        self._create_table(name, cur=rev_cur)

                    rev_cur.execute(
                        "INSERT INTO [{}] VALUES"
                        "(?, ?, ?)".format(name), (p, taxon, part_seq))

        concatenated_aln = AlignmentList([], db_con=db_con, db_cur=self.cur)
        alns = []

        # Create Alignment objects for each partition
        for name, part_range in self.partitions:

            name = "".join([x for x in name.split(".")[0] if x.isalnum()])

            if part_range[1]:

                for i in range(3):

                    part_len = (part_range[0][1] - part_range[0][0] + 1) / 3

                    taxa_list = taxa_list_master[name + str(i)]
                    taxa_idx = taxa_idx_master[name + str(i)]

                    add_alignment(name + str(i), part_len,
                                  taxa_list=taxa_list, taxa_idx=taxa_idx)

            else:

                part_len = part_range[0][1] - part_range[0][0] + 1

                taxa_list = taxa_list_master[name]
                taxa_idx = taxa_idx_master[name]

                add_alignment(name, part_len, taxa_list=taxa_list,
                              taxa_idx=taxa_idx)

        concatenated_aln.add_alignments(alns, ignore_paths=True)

        return concatenated_aln

    @setup_database
    def filter_codon_positions(self, position_list, table_in=None,
                               table_out="filter", ns=None,
                               use_main_table=False):
        """ Filter alignment according to codon positions.

        Filters codon positions from `Alignment` object with DNA sequence
        type. The `position_list` argument determines which codon positions
        will be stored. It should be a three element list, corresponding to
        the three codon position, and the positions that are saved are the
        ones with a True value.

        Parameters
        ----------
        position_list : list
            List of three bool elements that correspond to each codon position.
            Ex. [True, True, True] will save all positions while
            [True, True, False] will exclude the third codon position
        table_in : string
            Name of database table containing the alignment data that is
            used for this operation.
        table_out : string
            Name of database table where the final alignment will be inserted
            (default is "filter").
        use_main_table : bool
            If True, both `table_in` and `table_out` are ignore and the main
            table `Alignment.table_name` is used as the input and output
            table (default is False).
        ns : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        pbar : ProgressBar
            A ProgressBar object used to log the progress of TriSeq execution.

        See Also
        --------
        SetupDatabase
        """

        def index(length, pos):
            """
            index generator
            """
            for _ in range(0, length, 3):
                for j in pos:
                    if j:
                        yield 1
                    else:
                        yield 0

        # Create temporary table for storing unique sequences
        self._create_table(".codonfilter")

        # Create a new cursor to edit the database while iterating over
        # table_in
        cod_cur = self.con.cursor()

        for taxon, seq in self.iter_alignment(table_name=table_in):

            if ns:
                if ns.stop:
                    raise KillByUser("")

            seq = "".join(list(
                itertools.compress(seq, index(self.locus_length,
                                              position_list))))

            cod_cur.execute("INSERT INTO [.codonfilter] VALUES "
                            "(?, ?, ?)", (self.taxa_idx[taxon],
                                         taxon,
                                         seq))

        if self._table_exists(table_out):
            self.cur.execute("DROP TABLE [{}]".format(table_out))

        # Replace table_out with collapsed table
        self.cur.execute(
            "ALTER TABLE [.codonfilter] RENAME TO [{}]".format(
                table_out))

        self.locus_length = len(seq)

    @setup_database
    def code_gaps(self, table_out="gaps", table_in=None, use_main_table=False,
                  pbar=None, ns=None):
        """Code gap patterns as binary matrix at the end of alignment.

        This method codes gaps present in the alignment in binary format,
        according to the method of Simmons and Ochoterena (2000), to be read
        by phylogenetic programs such as MrBayes. The resultant alignment,
        however, can only be output in the Nexus format.

        Parameters
        ----------
        table_in : string
            Name of database table containing the alignment data that is
            used for this operation.
        table_out : string
            Name of database table where the final alignment will be inserted
            (default is "gaps").
        use_main_table : bool
            If True, both `table_in` and `table_out` are ignore and the main
            table `Alignment.table_name` is used as the input and output
            table (default is False).
        ns : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        pbar : ProgressBar
            A ProgressBar object used to log the progress of TriSeq execution.

        See Also
        --------
        SetupDatabase
        """

        self._set_pipes(ns, pbar, total=len(self.taxa_list))

        def gap_listing(sequence, gap_symbol="-"):
            """ Function that parses a sequence string and returns the
            position of indel events. The returned list is composed of
            tuples with the span of each indel """
            gap = "%s+" % gap_symbol
            span_regex = ""
            gap_list, seq_start = [], 0
            while span_regex is not None:
                span_regex = re.search(gap, sequence)
                if span_regex is not None and seq_start == 0:
                    gap_list.append(span_regex.span())
                    sequence = sequence[span_regex.span()[1] + 1:]
                    seq_start = span_regex.span()[1] + 1
                elif span_regex is not None and seq_start != 0:
                    gap_list.append((span_regex.span()[0] + seq_start,
                                     span_regex.span()[1] + seq_start))
                    sequence = sequence[span_regex.span()[1] + 1:]
                    seq_start += span_regex.span()[1] + 1
            return gap_list

        def gap_binary_generator(sequence, gap_list):
            """ This function contains the algorithm to construct the binary
             state block for the indel events """
            for cur_gap in gap_list:
                cur_gap_start, cur_gap_end = cur_gap
                if sequence[cur_gap_start:cur_gap_end] == "-" * \
                        (cur_gap_end - cur_gap_start) and \
                        sequence[cur_gap_start - 1] != "-" and \
                        sequence[cur_gap_end] != "-":
                    sequence += "1"

                elif sequence[cur_gap_start:cur_gap_end] == "-" * \
                        (cur_gap_end - cur_gap_start):

                    if sequence[cur_gap_start - 1] == "-" or \
                            sequence[cur_gap_end] == "-":
                        sequence += "-"

                elif sequence[cur_gap_start:cur_gap_end] != "-" * \
                        (cur_gap_end - cur_gap_start):
                    sequence += "0"
            return sequence

        complete_gap_list = []
        sequence_data = []

        # Get the complete list of unique gap positions in the alignment
        for p, (tx, seq) in enumerate(
                self.iter_alignment(table_name=table_in)):

            self._update_pipes(ns, pbar, value=p + 1,
                               msg="Fetching gaps for {}".format(tx))

            current_list = gap_listing(seq)
            complete_gap_list += [gap for gap in current_list if gap not in
                                  complete_gap_list]

        self._set_pipes(ns, pbar, total=len(self.taxa_list))

        # This will add the binary matrix of the unique gaps listed at the
        # end of each alignment sequence
        for p, (tx, seq) in enumerate(
                self.iter_alignment(table_name=table_in)):

            self._update_pipes(ns, pbar, value=p + 1,
                               msg="Adding gaps for {}".format(tx))

            final_seq = gap_binary_generator(seq, complete_gap_list)

            # Check if input and output tables are the same. If they are,
            # it means that the output table already exists and is being
            # updated
            if table_in == table_out:
                sequence_data.append((final_seq, self.taxa_idx[tx]))
            else:
                sequence_data.append((self.taxa_idx[tx], tx, final_seq))

        # Populate/modify table
        if table_in == table_out:
            self.cur.executemany("UPDATE [{}] SET seq=? WHERE txId=?".format(
                table_out), sequence_data)
        else:
            self.cur.executemany("INSERT INTO [{}] VALUES (?, ?, ?)".format(
                table_out), sequence_data)

        self.restriction_range = "%s-%s" % (int(self.locus_length),
                                            len(complete_gap_list) +
                                            int(self.locus_length) - 1)

        self._reset_pipes(ns)

    def _filter_terminals(self, table_in, table_out, ns=None):
        """Replace gaps at alignment's extremities with missing data.
        
        This method should not be called directly. Instead it is used by the
        `filter_missing_data` method.

        Parameters
        ----------
        table_in : string
            Name of database table containing the alignment data that is
            used for this operation.
        table_out : string
            Name of database table where the final alignment will be inserted
            (default is "collapsed").
        ns : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        """

        self._set_pipes(ns, None, total=len(self.taxa_list))

        self._create_table(".filterterminals")

        filt_cur = self.con.cursor()

        for p, (tx, seq) in enumerate(
                self.iter_alignment(table_name=table_in)):

            self._update_pipes(ns, None, value=p + 1,
                               msg="Filtering terminals")

            # Condition where the sequence only has gaps
            if not seq.strip("-"):
                seq = self.sequence_code[1] * len(seq)
                filt_cur.execute("INSERT INTO [.filterterminals] "
                                 "VALUES (?, ?, ?)",
                                 (self.taxa_idx[tx], tx, seq))
                continue

            seq = list(seq)
            counter, reverse_counter = 0, -1

            while seq[counter] == "-":
                seq[counter] = self.sequence_code[1]
                counter += 1

            while seq[reverse_counter] == "-":
                seq[reverse_counter] = self.sequence_code[1]
                reverse_counter -= 1

            filt_cur.execute("INSERT INTO [.filterterminals] "
                             "VALUES (?, ?, ?)",
                             (self.taxa_idx[tx], tx, "".join(seq)))

        # Check if input and output tables are the same. If they are,
        # drop the old table and replace with this new one
        if self._table_exists(table_out):
            self.cur.execute("DROP TABLE [{}]".format(table_out))

        self.cur.execute("ALTER TABLE [.filterterminals] "
                         "RENAME TO [{}]".format(table_out))

    def _filter_columns(self, gap_threshold, missing_threshold, table_in,
                        table_out, ns=None):
        """Filters alignment columns based on missing data content.
        
        Filters alignment columns based on the amount of missing data and
        gap content. If the calculated content is higher than a defined
        threshold, the column is removed from the final alignment. Settings
        `gap_threshold` and `missing_threshold` to 0 results in an alignment
        with no missing data, while setting them to 100 not remove any column.

        This method should not be called directly. Instead it is used by the
        `filter_missing_data` method.

        Parameters
        ----------
        gap_threshold : int
            Integer between 0 and 100 defining the percentage above which
            a column with that gap percentage is removed.
        missing_threshold : int
            Integer between 0 and 100 defining the percentage above which
            a column with that gap+missing percentage is removed.
        table_in : string
            Name of database table containing the alignment data that is
            used for this operation.
        table_out : string
            Name of database table where the final alignment will be inserted
            (default is "collapsed").
        ns : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        """

        self._set_pipes(ns, None, total=self.locus_length)

        taxa_number = float(len(self.taxa_list))

        filtered_cols = []

        self._create_table(".filtercolumns")

        filt_cur = self.con.cursor()

        # Creating the column list variable
        for p, column in enumerate(self.iter_columns(table_name=table_in)):

            self._update_pipes(ns, None, value=p + 1,
                               msg="Filtering columns")

            cadd = column.count

            # Calculating metrics
            gap_proportion = (float(cadd("-")) /
                              taxa_number) * float(100)
            missing_proportion = (float(cadd(self.sequence_code[1])) /
                                  taxa_number) * float(100)
            total_missing_proportion = gap_proportion + missing_proportion

            if gap_proportion <= gap_threshold and \
                    total_missing_proportion <= missing_threshold:

                filtered_cols.append(1)

            else:
                filtered_cols.append(0)

        for tx, seq in self.iter_alignment(table_name=table_in):

            if ns:
                if ns.stop:
                    raise KillByUser("")

            seq = "".join(compress(seq, filtered_cols))

            filt_cur.execute("INSERT INTO [.filtercolumns] "
                             "VALUES (?, ?, ?)",
                             (self.taxa_idx[tx], tx, seq))

        # Check if input and output tables are the same. If they are,
        # it means that the output table already exists and is being
        # updated
        if self._table_exists(table_out):
            self.cur.execute("DROP TABLE [{}];".format(table_out))

        self.cur.execute("ALTER TABLE [.filtercolumns] RENAME TO [{}]".format(
            table_out))

        self.locus_length = len(seq)

    @setup_database
    def filter_missing_data(self, gap_threshold, missing_threshold,
                            table_in=None, table_out="filter", ns=None,
                            use_main_table=False):
        """Filters alignment according to missing data.

        Filters gaps and true missing data from the alignment using tolerance
        thresholds for each type of missing data. Both thresholds are maximum
        percentages of sites in an alignment column containing the type of
        missing data. If gap_threshold=50, for example, alignment columns with
        more than 50% of sites with gaps are removed.

        Parameters
        ----------
        gap_threshold : int
            Integer between 0 and 100 defining the percentage above which
            a column with that gap percentage is removed.
        missing_threshold : int
            Integer between 0 and 100 defining the percentage above which
            a column with that gap+missing percentage is removed.
        table_in : string
            Name of database table containing the alignment data that is
            used for this operation.
        table_out : string
            Name of database table where the final alignment will be inserted
            (default is "filter").
        use_main_table : bool
            If True, both `table_in` and `table_out` are ignore and the main
            table `Alignment.table_name` is used as the input and output
            table (default is False).
        ns : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        """

        self._filter_terminals(table_in=table_in, table_out=table_out, ns=ns)

        # Update output table. This will make the _filter_columns method modify
        # the alignment produced in the _filter_terminals method.
        table_in = table_out

        self._filter_columns(gap_threshold, missing_threshold,
                             table_in=table_in, table_out=table_out, ns=ns)

    @staticmethod
    def _test_range(s, min_val, max_val):
        """Test if a given `s` integer in inside a specified range.

        Wrapper for the tests that determine whether a certain value
        (`s`) is within the range provided by `min_val` and `max_val`.
        Both of these arguments can be None, in which case there are not
        lower and/or upper boundaries, respectively.

        Parameters
        ----------
        s : int
            Test value
        min_val : int
            Minimum range allowed for the test value.
        max_val : int
            Maximum range allowed for the test value.

        Returns
        -------
        _ : bool
            True if `s` within boundaries. False if not.
        """

        # If both values were specified, check if s is within range
        if max_val is not None and min_val is not None \
                and s >= min_val and s <= max_val:
            return True
        # If only min_val was specified, check if s is higher
        elif max_val is None and s >= min_val:
            return True
        # If only max_val was specified, check if s is lower
        elif min_val is None and s <= max_val:
            return True
        else:
            return False

    def filter_segregating_sites(self, min_val, max_val, table_in=None,
                                    ns=None, pbar=None):
        """Checks if the segregating sites from alignment are within range.

        Evaluates the number of segregating sites of the current alignment
        and returns True if it falls between the range specified by the
        `min_val` and `max_val` arguments.

        Parameters
        ----------
        min_val : int
            Minimum number of segregating sites for the alignment to pass.
            Can be None, in which case there is no lower bound.
        max_val : int
            Maximum number of segregating sites for the alignment to pass.
            Can be None, in which case there is no upper bound.
        table_in : string
            Name of database table containing the alignment data that is
            used for this operation.
        ns : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        pbar : ProgressBar
            A ProgressBar object used to log the progress of TriSeq execution.

        Returns
        -------
        _ : bool
            True if the number of segregating sites are within the specified
            range. False if not.

        See Also
        --------
        filter_informative_sites
        """

        if pbar:
            pbar.max_value = self.locus_length

        # Counter for segregating sites
        s = 0

        # Creating the column list variable
        for p, column in enumerate(self.iter_columns_uniq(
                table_name=table_in)):

            if ns:
                if ns.stop:
                    raise KillByUser("")

            if pbar:
                pbar.update(p + 1)

            v = len([i for i in column if i not in
                     [self.sequence_code[1], "-"]])

            if v == 1:
                continue

            if v > 1:
                s += 1

            # Add these tests so that the method may exit earlier if the
            # conditions are met, precluding the analysis of the entire
            # alignment
            if min_val and s >= min_val and max_val is None:
                return True

            if max_val and s > max_val:
                return False

        return self._test_range(s, min_val, max_val)

    def filter_informative_sites(self, min_val, max_val, table_in=None,
                                    ns=None, pbar=None):
        """Checks if the variable sites from alignment are within range.

        Similar to `filter_segregating_sites` method, but only considers
        informative sites (variable sites present in more than 2 taxa).

        Parameters
        ----------
        min_val : int
            Minimum number of informative sites for the alignment to pass.
            Can be None, in which case there is no lower bound.
        max_val : int
            Maximum number of informative sites for the alignment to pass.
            Can be None, in which case there is no upper bound.
        table_in : string
            Name of database table containing the alignment data that is
            used for this operation.
        ns : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        pbar : ProgressBar
            A ProgressBar object used to log the progress of TriSeq execution.

        Returns
        -------
        _ : bool
            True if the number of informative sites are within the specified
            range. False if not.

        See Also
        --------
        filter_segregating_sites
        """

        if pbar:
            pbar.max_value = self.locus_length

        # Counter for informative sites
        s = 0

        # Creating the column list variable
        for p, column in enumerate(self.iter_columns(table_name=table_in)):

            if pbar:
                pbar.update(p + 1)

            if ns:
                if ns.stop:
                    raise KillByUser("")

            column = Counter([i for i in column if i not in
                              [self.sequence_code[1], "-"]])

            if column:

                # Skip if column has no variation
                if len(column) == 1:
                    continue

                # Delete most common
                del column[column.most_common()[0][0]]

                # If any of the remaining sites is present in more than two
                # taxa score the site as informative
                if any([x >= 2 for x in column.values()]):
                    s += 1

                # Add these tests so that the method may exit earlier if the
                # conditions are met, precluding the analysis of the entire
                # alignment
                if min_val and s >= min_val and max_val is None:
                    return True

                if max_val and s > max_val:
                    return False

        return self._test_range(s, min_val, max_val)

    def _get_interleave_data(self, table_name=None, table_suffix=None,
                             ns_pipe=None, pbar=None):
        """Builds an interleave matrix on the database.

        When the user requests an interleave output format, this method
        rearranges the data matrix (which is leave in the database) in
        interleave format in a new table. This should be performed only once,
        even when multiple formats are specified in interleave. The first one
        calls this class, and the subsequent ones use the this table.

        Parameters
        ----------
        ns_pipe : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        pbar : ProgressBar
            A ProgressBar object used to log the progress of TriSeq execution.
        table_suffix : string
            Suffix of the table from where the sequence data is fetched.
            The suffix is append to `Alignment.table_name`.
        table_name : string
            Name of the table from where the sequence data is fetched.
        """

        if self.cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND"
                " name='.interleavedata'").fetchall():

            self.cur.execute("DELETE FROM [.interleavedata]")

        else:
            self.cur.execute("CREATE TABLE [.interleavedata]("
                             "taxon TEXT,"
                             "slice INT,"
                             "seq TEXT)")
            self.cur.execute("CREATE INDEX interindex ON "
                            "[.interleavedata](slice)")

        temp_cur = self.con.cursor()

        for c, (tx, seq) in enumerate(self.iter_alignment(
                table_name=table_name,
                table_suffix=table_suffix)):

            self._update_pipes(ns_pipe, pbar,
                               value=c + 1)

            counter = 0

            for i in xrange(90, self.locus_length, 90):

                temp_cur.execute("INSERT INTO [.interleavedata] VALUES"
                                 " (?, ?, ?)", (tx, i, seq[counter:i]))
                # temp_storage[p].append(seq[counter:i])
                counter = i

            try:
                if self.locus_length % 90:
                    i += 1
                    temp_cur.execute("INSERT INTO [.interleavedata] VALUES"
                                     " (?, ?, ?)", (tx, i, seq[counter:]))
                    # temp_storage[p].append(seq[c:],)
            # This likely means that the alignment is less than
            # 90 characters long. In this case, append to the storage's
            # 0 index
            except UnboundLocalError:
                temp_cur.execute("INSERT INTO [.interleavedata] VALUES "
                                 "(?, ?, ?)", (tx, 0, seq))

        return True

    def _write_fasta(self, output_file, **kwargs):
        """Writes `Alignment` object into fast output format.

        Parameters
        ----------
        output_file : str
            Name of the output file. If using `ns_pipe` in TriFusion,
            it will prompt the user if the output file already exists.
        interleave : bool
            Determines whether the output alignment
            will be in leave (False) or interleave (True) format. Not all
            output formats support this option.
        ld_hat : bool
            If True, the fasta output format will include a first line
            compliant with the format of LD Hat and will truncate sequence
            names and sequence length per line accordingly.
        ns_pipe : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        pbar : ProgressBar
            A ProgressBar object used to log the progress of TriSeq execution.
        table_suffix : string
            Suffix of the table from where the sequence data is fetched.
            The suffix is append to `Alignment.table_name`.
        table_name : string
            Name of the table from where the sequence data is fetched.
        """

        # Get relevant keyword arguments
        ld_hat = kwargs.get("ld_hat", False)
        interleave = kwargs.get("interleave", False)
        table_suffix = kwargs.get("table_suffix", None)
        table_name = kwargs.get("table_name", None)
        ns_pipe = kwargs.get("ns_pipe", None)
        pbar = kwargs.get("pbar", None)

        out_file = open("{}.fas".format(output_file), "w")

        # If LD HAT sub format has been specificed, write the first line
        # containing the number of sequences, sites and genotype phase
        if ld_hat:
            out_file.write("{} {} {}\n".format(
                len(self.taxa_list), self.locus_length, "2"))

        self._set_pipes(ns_pipe, pbar, total=len(self.taxa_list),
                        msg="Writing fasta output")

        for p, (taxon, seq) in enumerate(self.iter_alignment(
                table_name=table_name, table_suffix=table_suffix)):

            self._update_pipes(ns_pipe, pbar, value=p + 1)

            if ld_hat:
                # Truncate sequence name to 30 characters
                out_file.write(">%s\n" % (taxon[:30]))
                # Limit each sequence line to 2000 characters
                if len(seq) > 2000:
                    for i in range(0, len(seq), 2000):
                        out_file.write("%s\n" % (seq[i:i + 2000].upper()))
            elif interleave:
                out_file.write(">%s\n" % taxon)
                counter = 0
                for i in range(90, self.locus_length, 90):
                    out_file.write("%s\n" % seq[counter:i])
                    counter = i
                else:
                    out_file.write("%s\n" % seq[counter:])
            else:
                out_file.write(">%s\n%s\n" % (taxon, seq.upper()))

        out_file.close()

    def _write_phylip(self, output_file, **kwargs):
        """Writes `Alignment` object into phylip format.

        Parameters
        ----------
        output_file : str
            Name of the output file. If using `ns_pipe` in TriFusion,
            it will prompt the user if the output file already exists.
        tx_space_phy : int
            Space (in characters) provided for the taxon name in phylip
            format (default is 40).
        cut_space_phy : int
            Set the maximum allowed character length for taxon names in
            phylip format. Longer  names are truncated (default is 39).
        interleave : bool
            Determines whether the output alignment
            will be in leave (False) or interleave (True) format. Not all
            output formats support this option.
        phy_truncate_names : bool
            If True, taxon names in phylip format are truncated to a maximum
            of 10 characters (default is False).
        partition_file : bool
            If True, the auxiliary partitions file will be written (default
            is True).
        model_phylip : str
            Substitution model for the auxiliary partition file of phylip
            format, compliant with RAxML.
        ns_pipe : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        pbar : ProgressBar
            A ProgressBar object used to log the progress of TriSeq execution.
        table_suffix : string
            Suffix of the table from where the sequence data is fetched.
            The suffix is append to `Alignment.table_name`.
        table_name : string
            Name of the table from where the sequence data is fetched.
        """

        # Get relevant keyword arguments
        interleave = kwargs.get("interleave", False)
        tx_space_phy = kwargs.get("tx_space_phy", 40)
        cut_space_phy = kwargs.get("cut_space_phy", 39)
        phy_truncate_names = kwargs.get("phy_truncate_names", False)
        partition_file = kwargs.get("partition_file", None)
        model_phylip = kwargs.get("model_phylip", None)
        table_suffix = kwargs.get("table_suffix", None)
        table_name = kwargs.get("table_name", None)
        ns_pipe = kwargs.get("ns_pipe", None)
        pbar = kwargs.get("pbar", None)

        # Change taxa space if phy_truncate_names option is set to True
        if phy_truncate_names:
            cut_space_phy = 10

        out_file = open("{}.phy".format(output_file), "w")

        out_file.write("{} {}\n".format(len(self.taxa_list),
                                        self.locus_length))
        if interleave:

            self._set_pipes(ns_pipe, pbar,
                            total=len(self.taxa_list),
                            msg="Writing phylip output")

            if not self.interleave_data:
                self.interleave_data = self._get_interleave_data(
                    table_name=table_name, table_suffix=table_suffix,
                    ns_pipe=ns_pipe, pbar=pbar
                )

            write_tx = True
            prev = 90
            for tx, p, seq in self.cur.execute(
                    "SELECT taxon, slice, seq FROM [.interleavedata] "
                    "ORDER BY slice"):

                if p != prev:
                    out_file.write("\n")
                    prev = p
                    write_tx = False

                if write_tx:
                    out_file.write("{} {}\n".format(
                        tx[:cut_space_phy].ljust(
                            tx_space_phy), seq.upper()))
                else:
                    out_file.write(seq.upper() + "\n")

        else:

            self._set_pipes(ns_pipe, pbar,
                            total=len(self.taxa_list),
                            msg="Writing phylip output")

            for p, (taxon, seq) in enumerate(self.iter_alignment(
                    table_name=table_name, table_suffix=table_suffix)):
                self._update_pipes(ns_pipe, pbar,
                                   value=p + 1)

                out_file.write("%s %s\n" % (
                    taxon[:cut_space_phy].ljust(tx_space_phy),
                    seq.upper()))

        # In case there is a concatenated alignment being written
        if not self.partitions.is_single() and partition_file:
            partition_file = open(output_file + "_part.File", "w")
            for name, lrange in self.partitions:
                # Get model from app if it exists and there are no codon
                # positions
                model = model_phylip if self.partitions.models[name] == \
                                        [None] or len(
                    self.partitions.models[name][1]) > 1 \
                    else self.partitions.models[name][1][0]
                partition_file.write("%s, %s = %s\n" % (
                    model if model else
                    self.sequence_code[0], name,
                    "-".join([str(x + 1) for x in
                              lrange[0]])))
            partition_file.close()

        out_file.close()

    def _write_nexus(self, output_file, **kwargs):
        """Writes `Alignment` object into nexus file format.

        Parameters
        ----------
        output_format : list
            List with the output formats to generate. Options are:
            {"fasta", "phylip", "nexus", "stockholm", "gphocs", "ima2"}.
        tx_space_nex : int
            Space (in characters) provided for the taxon name in nexus
            format (default is 40).
        cut_space_nex : int
            Set the maximum allowed character length for taxon names in
            nexus format. Longer  names are truncated (default is 39).
        interleave : bool
            Determines whether the output alignment
            will be in leave (False) or interleave (True) format. Not all
            output formats support this option.
        gap : str
            Symbol for alignment gaps (default is '-').
        use_charset : bool
            If True, partitions from the `Partitions` object will be written
            in the nexus output format (default is True).
        use_nexus_models : bool
            If True, writes the partitions charset block in nexus format.
        outgroup_list : list
            Specify list of taxon names that will be defined as the outgroup
            in the nexus output format. This may be useful for analyses with
            MrBayes or other software that may require outgroups.
        ns_pipe : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        pbar : ProgressBar
            A ProgressBar object used to log the progress of TriSeq execution.
        table_suffix : string
            Suffix of the table from where the sequence data is fetched.
            The suffix is append to `Alignment.table_name`.
        table_name : string
            Name of the table from where the sequence data is fetched.
        """

        # Get relevant keyword arguments
        interleave = kwargs.get("interleave", False)
        tx_space_nex = kwargs.get("tx_space_nex", 40)
        cut_space_nex = kwargs.get("cut_space_nex", 39)
        gap = kwargs.get("gap", "-")
        use_charset = kwargs.get("use_charset", True)
        use_nexus_models = kwargs.get("use_nexus_models", True)
        outgroup_list = kwargs.get("outgroup_list", None)
        table_suffix = kwargs.get("table_suffix", None)
        table_name = kwargs.get("table_name", None)
        ns_pipe = kwargs.get("ns_pipe", None)
        pbar = kwargs.get("pbar", None)

        out_file = open("{}.nex".format(output_file), "w")

        # This writes the output in interleave format
        if interleave:

            self._set_pipes(ns_pipe, pbar, total=len(self.taxa_list),
                            msg="Writing nexus output")

            if self.restriction_range is not None:
                out_file.write("#NEXUS\n\nBegin data;\n\tdimensions "
                               "ntax=%s nchar=%s ;\n\tformat datatype="
                               "mixed(%s:1-%s, restriction:%s) interleave="
                               "yes gap=%s missing=%s ;\n\tmatrix\n" %
                               (len(self.taxa_list),
                                self.locus_length,
                                self.sequence_code[0].upper(),
                                self.locus_length - 1,
                                self.restriction_range,
                                gap,
                                self.sequence_code[1].upper()))
            else:
                out_file.write("#NEXUS\n\nBegin data;\n\tdimensions "
                               "ntax=%s nchar=%s ;\n\tformat datatype=%s "
                               "interleave=yes gap=%s missing=%s ;\n\t"
                               "matrix\n" %
                               (len(self.taxa_list),
                                self.locus_length,
                                self.sequence_code[0].upper(),
                                gap,
                                self.sequence_code[1].upper()))

            if not self.interleave_data:
                self.interleave_data = self._get_interleave_data(
                    table_suffix=table_suffix, table_name=table_name,
                    ns_pipe=ns_pipe, pbar=pbar
                )

            # Iterate over interleave data
            prev = 0
            for tx, p, seq in self.cur.execute(
                    "SELECT taxon, slice, seq FROM [.interleavedata] "
                    "ORDER BY slice"):

                if p != prev:
                    out_file.write("\n")
                    prev = p

                out_file.write("{} {}\n".format(
                    tx[:cut_space_nex].ljust(tx_space_nex),
                    seq.upper()))

            out_file.write(";\n\tend;")

        # This writes the output in leave format (default)
        else:

            self._set_pipes(ns_pipe, pbar, total=len(self.taxa_list),
                            msg="Writing nexus output")

            if self.restriction_range is not None:
                out_file.write("#NEXUS\n\nBegin data;\n\tdimensions "
                               "ntax=%s nchar=%s ;\n\tformat datatype=mixed"
                               "(%s:1-%s, restriction:%s) interleave=no "
                               "gap=%s missing=%s ;\n\tmatrix\n" %
                               (len(self.taxa_list),
                                self.locus_length,
                                self.sequence_code[0],
                                self.locus_length - 1,
                                self.restriction_range,
                                gap,
                                self.sequence_code[1].upper()))
            else:
                out_file.write("#NEXUS\n\nBegin data;\n\tdimensions ntax=%s"
                               " nchar=%s ;\n\tformat datatype=%s "
                               "interleave=no gap=%s missing=%s ;\n\t"
                               "matrix\n" % (
                                   len(self.taxa_list),
                                   self.locus_length,
                                   self.sequence_code[0],
                                   gap, self.sequence_code[1].upper()))

            for p, (taxon, seq) in enumerate(self.iter_alignment(
                    table_name=table_name, table_suffix=table_suffix)):
                self._update_pipes(ns_pipe, pbar, value=p + 1)

                out_file.write("%s %s\n" % (taxon[:cut_space_nex].ljust(
                    tx_space_nex), seq))

            out_file.write(";\n\tend;")

        if use_charset:
            # Writing partitions, if any
            if not self.partitions.is_single():
                out_file.write("\nbegin mrbayes;\n")
                p = 0
                # Full gene partitions
                for name, lrange in self.partitions:
                    # If there are codon partitions, write those
                    if lrange[1]:
                        for i in lrange[1]:
                            out_file.write("\tcharset %s_%s = %s-%s\\3;\n" %
                                           (name, i + 1, i + 1,
                                            lrange[0][1] + 1))
                            p += 1
                    else:
                        out_file.write("\tcharset %s = %s-%s;\n" %
                                       (name, lrange[0][0] + 1,
                                        lrange[0][1] + 1))
                        p += 1

                out_file.write("\tpartition part = %s: %s;\n\tset "
                               "partition=part;\nend;\n" %
                               (p, ", ".join([name for name in
                                              self.partitions.get_partition_names()])))

        # Write models, if any
        if use_nexus_models:
            out_file.write("\nbegin mrbayes;\n")
            i = 1
            for name, models in self.partitions.models.items():
                if models[1]:
                    for m in models[1]:
                        if m:
                            m_str = self.partitions._models["mrbayes"][m]
                            if not self.partitions.is_single():
                                out_file.write("applyto=({}) "
                                               "lset ".format(i) +
                                               " ".join(m_str) + "\n")
                            else:
                                out_file.write("lset " +
                                               " ".join(m_str) + "\n")
                        i += 1
            out_file.write("end;\n")

        # In case outgroup taxa are specified
        if outgroup_list is not None:

            # This assures that only the outgroups present in the current
            #  file are written
            compliant_outgroups = [taxon for taxon in outgroup_list
                                   if taxon in self.taxa_list]
            if compliant_outgroups is not []:
                out_file.write("\nbegin mrbayes;\n\toutgroup %s\nend;\n" %
                               (" ".join(compliant_outgroups)))

        out_file.close()

    def _write_stockholm(self, output_file, **kwargs):
        """Writes `Alignment` object into stockholm file format.

        Parameters
        ----------
        ns_pipe : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        pbar : ProgressBar
            A ProgressBar object used to log the progress of TriSeq execution.
        table_suffix : string
            Suffix of the table from where the sequence data is fetched.
            The suffix is append to `Alignment.table_name`.
        table_name : string
            Name of the table from where the sequence data is fetched.
        """

        table_suffix = kwargs.get("table_suffix", None)
        table_name = kwargs.get("table_name", None)
        ns_pipe = kwargs.get("ns_pipe", None)
        pbar = kwargs.get("pbar", None)

        self._set_pipes(ns_pipe, pbar, total=len(self.taxa_list),
                        msg="Writing stockholm output")

        out_file = open("{}.stockholm".format(output_file), "w")

        out_file.write("# STOCKHOLM V1.0\n")

        for p, (taxon, seq) in enumerate(self.iter_alignment(
                table_name=table_name, table_suffix=table_suffix)):
            self._update_pipes(ns_pipe, pbar, value=p + 1)

            out_file.write("{}\t{}\n".format(taxon, seq))

        out_file.write("//\n")
        out_file.close()

    def _write_gphocs(self, output_file, **kwargs):
        """Writes `Alignment` object into gphocs file format.

        Parameters
        ----------
        ns_pipe : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        pbar : ProgressBar
            A ProgressBar object used to log the progress of TriSeq execution.
        table_suffix : string
            Suffix of the table from where the sequence data is fetched.
            The suffix is append to `Alignment.table_name`.
        table_name : string
            Name of the table from where the sequence data is fetched.
        """

        table_suffix = kwargs.get("table_suffix", None)
        table_name = kwargs.get("table_name", None)
        ns_pipe = kwargs.get("ns_pipe", None)
        pbar = kwargs.get("pbar", None)

        out_file = open("{}.txt".format(output_file), "w")

        # Write number of loci
        out_file.write("%s\n" % (len(self.partitions.partitions)))

        if not self.partitions.is_single():

            self._set_pipes(ns_pipe, pbar,
                            total=len(self.partitions.partitions),
                            msg="Writing gphocs output")

            for p, (name, lrange) in enumerate(
                    self.partitions.partitions.items()):

                self._update_pipes(ns_pipe, pbar, value=p + 1)

                lrange = lrange[0]
                out_file.write("%s %s %s\n" % (name, len(self.taxa_list),
                                               lrange[1] - lrange[0]))

                for taxon, seq in self.iter_alignment(
                        table_name=table_name,
                        table_suffix=table_suffix):
                    out_file.write("%s\t%s\n" % (taxon,
                                                 seq[lrange[0]:lrange[1]]))

        else:

            self._set_pipes(ns_pipe, pbar, total=len(self.taxa_list),
                            msg="Writing gphocs output")

            out_file.write("%s %s %s\n" % (self.sname,
                                           len(self.taxa_list),
                                           self.locus_length))

            for p, (taxon, seq) in enumerate(self.iter_alignment(
                    table_name=table_name,
                    table_suffix=table_suffix)):
                self._update_pipes(ns_pipe, pbar, value=p + 1)

                out_file.write("%s\t%s\n" % (taxon, seq))

        out_file.close()

    def _write_ima2(self, output_file, **kwargs):
        """Writes `Alignment` object into ima2 file format.

        Parameters
        ----------
        tx_space_ima2 : int
            Space (in characters) provided for the taxon name in ima2
            format (default is 10).
        cut_space_ima2 : int
            Set the maximum allowed character length for taxon names in
            ima2 format. Longer  names are truncated (default is 8).
        ima2_params : list
            A list with the additional information required for the ima2
            output format. The list should contains the following information:

               1. (str) path to file containing the species and populations.
               2. (str) Population tree in newick format, e.g. (0,1):2.
               3. (str) Mutational model for all alignments.
               4. (str) inheritance scalar.

        ns_pipe : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        pbar : ProgressBar
            A ProgressBar object used to log the progress of TriSeq execution.
        table_suffix : string
            Suffix of the table from where the sequence data is fetched.
            The suffix is append to `Alignment.table_name`.
        table_name : string
            Name of the table from where the sequence data is fetched.
        """

        tx_space_ima2 = kwargs.get("tx_space_ima2", 10)
        cut_space_ima2 = kwargs.get("cut_space_ima2", 8)
        ima2_params = kwargs.get("ima2_params", None)
        table_suffix = kwargs.get("table_suffix", None)
        table_name = kwargs.get("table_name", None)
        ns_pipe = kwargs.get("ns_pipe", None)
        pbar = kwargs.get("pbar", None)

        population_file = ima2_params[0]
        population_tree = ima2_params[1]
        mutational_model = ima2_params[2]
        inheritance_scalar = ima2_params[3]

        # Get information on which species belong to each population from
        #  the populations file
        population_handle = open(population_file)
        population_storage = OrderedDict()
        for line in population_handle:
            taxon, population = line.strip().split()
            try:
                population_storage[population.strip()].append(taxon)
            except KeyError:
                population_storage[population.strip()] = [taxon]

        # Write the general header of the IMa2 input file
        out_file = open("{}.txt".format(output_file), "w")
        # First line with general description
        out_file.write("Input file for IMa2 using %s alignments\n"
                       "%s\n"  # Line with number of loci
                       "%s\n"  # Line with name of populations
                       "%s\n"  # Line with population string
                       % (len(self.partitions.partitions),
                          len(population_storage),
                          " ".join(population_storage.keys()),
                          population_tree))

        if not self.partitions.is_single():

            self._set_pipes(ns_pipe, pbar,
                            total=len(self.partitions.partitions),
                            msg="Writing IMa2 output")

            # Write each locus
            for p, (partition, lrange) in enumerate(self.partitions):

                self._update_pipes(ns_pipe, pbar, value=p + 1)

                # Retrieving taxon names and sequence data. This step is
                # the first because it will enable the removal of species
                # containing only missing data.
                new_alignment = []

                # This temporary ordered dictionary is created so that
                # the number of taxa per populations is corrected in
                # each locus
                current_locus_populations = OrderedDict(
                    (x, []) for x in population_storage)

                for population, taxa_list in population_storage.items():
                    for taxon in taxa_list:
                        # This try statement catches common errors, such as
                        #  providing a species in the mapping file that
                        # does not exist in the alignment
                        try:
                            seq = self.get_sequence(
                                taxon, table_name=table_name,
                                table_suffix=table_suffix)[
                                  lrange[0][0]:lrange[0][1]].upper()
                        except KeyError:
                            print("Taxon %s provided in auxiliary "
                                  "population mapping file is not found "
                                  "in the alignment")
                            raise SystemExit

                        if seq.replace(self.sequence_code[1], "") != "":
                            new_alignment.append((taxon[:cut_space_ima2]
                                                  .ljust(tx_space_ima2),
                                                  seq))

                            current_locus_populations[population] \
                                .append(taxon)

                # Write the header of each partition
                out_file.write("%s %s %s %s %s\n" % (
                    partition,
                    " ".join([str(len(x)) for x in
                              list(current_locus_populations.values())]),
                    (lrange[0][1]) - lrange[0][0],
                    mutational_model,
                    inheritance_scalar))

                # Write sequence data according to the order of the
                # population mapping file
                for taxon, seq in new_alignment:
                    out_file.write("%s%s\n" % (taxon, seq))

        if self.partitions.is_single():

            self._set_pipes(ns_pipe, pbar,
                            total=len(population_storage),
                            msg="Writing IMa2 output")

            # Write the header for the single
            out_file.write("%s %s %s %s %s\n" % (
                partition,
                " ".join(population_storage.values()),
                self.locus_length,
                mutational_model,
                inheritance_scalar))

            # Write sequence data
            for p, (population, taxa_list) in enumerate(
                    population_storage.items()):

                self._update_pipes(ns_pipe, pbar,
                                   value=p + 1)

                for taxon in taxa_list:
                    seq = self.get_sequence(
                        taxon, table_name=table_name,
                        table_suffix=table_suffix).upper()
                    out_file.write("%s%s\n" %
                                   (
                                   taxon[:cut_space_ima2].ljust(tx_space_ima2),
                                   seq))

    def _write_mcmctree(self, output_file, **kwargs):
        """Writes `Alignment` object into mcmctree file format.

        Parameters
        ----------
        tx_space_phy : int
            Space (in characters) provided for the taxon name in phylip
            format (default is 40).
        cut_space_phy : int
            Set the maximum allowed character length for taxon names in
            phylip format. Longer  names are truncated (default is 39).
        ns_pipe : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        pbar : ProgressBar
            A ProgressBar object used to log the progress of TriSeq execution.
        table_suffix : string
            Suffix of the table from where the sequence data is fetched.
            The suffix is append to `Alignment.table_name`.
        table_name : string
            Name of the table from where the sequence data is fetched.
        """

        tx_space_phy = kwargs.get("tx_space_phy", 40)
        cut_space_phy = kwargs.get("cut_space_phy", 39)
        table_suffix = kwargs.get("table_suffix", None)
        table_name = kwargs.get("table_name", None)
        ns_pipe = kwargs.get("ns_pipe", None)
        pbar = kwargs.get("pbar", None)

        out_file = open("{}_mcmctree.phy".format(output_file), "w")
        taxa_number = len(self.taxa_list)

        if self.partitions.is_single() is False:

            self._set_pipes(ns_pipe, pbar,
                            total=len(self.partitions.partitions),
                            msg="Writing mcmctree output")

            for p, lrange in enumerate(
                    self.partitions.partitions.values()):

                self._update_pipes(ns_pipe, pbar, value=p + 1)

                lrange = lrange[0]
                out_file.write("%s %s\n" % (taxa_number,
                                            (lrange[1] - (lrange[0]))))

                for taxon, seq in self.iter_alignment(
                        table_name=table_name, table_suffix=table_suffix):
                    out_file.write("%s  %s\n" % (
                        taxon[:cut_space_phy].ljust(
                            tx_space_phy),
                        seq[lrange[0]:lrange[1]].upper()))
        else:

            self._set_pipes(ns_pipe, pbar, total=len(self.taxa_list),
                            msg="Writing mcmctree output")

            out_file.write("%s %s\n" % (taxa_number, self.locus_length))
            for p, (taxon, seq) in enumerate(self.iter_alignment(
                    table_name=table_name, table_suffix=table_suffix)):
                self._update_pipes(ns_pipe, pbar, value=p + 1)

                out_file.write("%s  %s\n" % (
                    taxon[:cut_space_phy].ljust(tx_space_phy),
                    seq.upper()))

        out_file.close()

    def write_to_file(self, output_format, output_file, **kwargs):
        """Writes alignment data into an output file.

        Writes the alignment object into a specified output file,
        automatically adding the extension, according to the specified
        output formats.

        This function supports the writing of both converted (no partitions)
        and concatenated (partitioned files). The choice between these modes
        is determined by the Partitions object associated with the Alignment
        object. If it contains multiple partitions, it will produce a
        concatenated alignment and the auxiliary partition files where
        necessary. Otherwise it will treat the alignment as a single partition.

        Parameters
        ----------
        output_format : list
            List with the output formats to generate. Options are:
            {"fasta", "phylip", "nexus", "stockholm", "gphocs", "ima2"}.
        output_file : str
            Name of the output file. If using `ns_pipe` in TriFusion,
            it will prompt the user if the output file already exists.
        tx_space_nex : int
            Space (in characters) provided for the taxon name in nexus
            format (default is 40).
        tx_space_phy : int
            Space (in characters) provided for the taxon name in phylip
            format (default is 40).
        tx_space_ima2 : int
            Space (in characters) provided for the taxon name in ima2
            format (default is 10).
        cut_space_nex : int
            Set the maximum allowed character length for taxon names in
            nexus format. Longer  names are truncated (default is 39).
        cut_space_phy : int
            Set the maximum allowed character length for taxon names in
            phylip format. Longer  names are truncated (default is 39).
        cut_space_ima2 : int
            Set the maximum allowed character length for taxon names in
            ima2 format. Longer  names are truncated (default is 8).
        interleave : bool
            Determines whether the output alignment
            will be in leave (False) or interleave (True) format. Not all
            output formats support this option.
        gap : str
            Symbol for alignment gaps (default is '-').
        model_phylip : str
            Substitution model for the auxiliary partition file of phylip
            format, compliant with RAxML.
        outgroup_list : list
            Specify list of taxon names that will be defined as the outgroup
            in the nexus output format. This may be useful for analyses with
            MrBayes or other software that may require outgroups.
        ima2_params : list
            A list with the additional information required for the ima2
            output format. The list should contains the following information:

               1. (str) path to file containing the species and populations.
               2. (str) Population tree in newick format, e.g. (0,1):2.
               3. (str) Mutational model for all alignments.
               4. (str) inheritance scalar.

        use_charset : bool
            If True, partitions from the `Partitions` object will be written
            in the nexus output format (default is True).
        partition_file : bool
            If True, the auxiliary partitions file will be written (default
            is True).
        output_dir : str
            If provided, the output file will be written on the specified
            directory.
        phy_truncate_names : bool
            If True, taxon names in phylip format are truncated to a maximum
            of 10 characters (default is False).
        ld_hat : bool
            If True, the fasta output format will include a first line
            compliant with the format of LD Hat and will truncate sequence
            names and sequence length per line accordingly.
        use_nexus_models : bool
            If True, writes the partitions charset block in nexus format.
        ns_pipe : multiprocesssing.Manager.Namespace
            A Namespace object used to communicate with the main thread
            in TriFusion.
        pbar : ProgressBar
            A ProgressBar object used to log the progress of TriSeq execution.
        table_suffix : string
            Suffix of the table from where the sequence data is fetched.
            The suffix is append to `Alignment.table_name`.
        table_name : string
            Name of the table from where the sequence data is fetched.
        """

        # Get relevant keyword arguments
        ns_pipe = kwargs.get("ns_pipe", None)
        model_phylip = kwargs.get("model_phylip", None)

        write_methods = {
            "fasta": self._write_fasta,
            "phylip": self._write_phylip,
            "nexus": self._write_nexus,
            "stockholm": self._write_stockholm,
            "gphocs": self._write_gphocs,
            "ima2": self._write_ima2,
            "mcmctree": self._write_mcmctree
        }

        # If a specific output directory is provided, the output file will be
        # written there
        output_dir = kwargs.pop("output_dir", None)
        if output_dir:
            output_file = join(output_dir, output_file)
            if not exists(output_dir):
                os.makedirs(output_dir)

        # Stores suffixes for each format
        format_ext = {"ima2": ".txt",
                      "mcmctree": "_mcmctree.phy",
                      "phylip": ".phy",
                      "nexus": ".nex",
                      "fasta": ".fas",
                      "stockholm": ".stockholm",
                      "gphocs": ".txt"}

        # Check if any output file already exist. If so, issue a warning
        # through the app or terminal pipes
        for f in output_format:
            fname = output_file + format_ext[f]
            if exists(fname):

                # File exists issue warning through appropriate pipe
                if ns_pipe:
                    if not ns_pipe.apply_all:
                        ns_pipe.file_dialog = fname
                        while not ns_pipe.status:
                            pass

                # When the dialog has been close, check if file is to be
                # skipped or overwritten
                if ns_pipe:
                    if ns_pipe.status == "skip":
                        # Skip file
                        return

        # Reset pipes, if any
        if ns_pipe:
            ns_pipe.status = None

        # This will determine the default model value. GTR for nucleotides
        # and LG for proteins
        if not model_phylip:
            if self.sequence_code[0] == "DNA":
                model_phylip = "GTR"
            else:
                model_phylip = "LG"
            kwargs["model_phylip"] = model_phylip

        # Checks if there is any other format besides Nexus if the
        # alignment's gap have been coded
        if self.restriction_range is not None:
            if output_format != ["nexus"]:
                return 0
        else:
            pass

        for fmt in output_format:
            write_methods[fmt](output_file, **kwargs)

        self._reset_pipes(ns_pipe)

        # Remove temporary interleave data, if present
        if self._table_exists(".interleavedata"):
            self.cur.execute("DROP TABLE [.interleavedata]")


class AlignmentList(Base):
    """
    At the most basic instance, this class contains a list of Alignment
    objects upon which several methods can be applied. It only requires either
    a list of alignment files or. It inherits methods from Base and
    Alignment classes for the write_to_file methods.
    """

    def __init__(self, alignment_list, dest=None,
                 sql_db=None, db_cur=None, db_con=None, pbar=None):
        """
        :param alignment_list: List of Alignment objects
        :param dest: String. Path to temporary directory that will store
        the sequence data of each alignment object
        :param sql_db: string. Path to sqlite database file where sequence data
        will be stored
        :param db_cur: sqlite cursors. If provided, along with the db_con
        argument, it provides the necessary hooks to the sqlite database and
        the sql_db argument will be ignored. Therefore, no new connection will
        be open
        :param db_con:sqlite connection. If provided, along with the db_cur
        argument, it provides the necessary hooks to the sqlite database and
        the sql_db argument will be ignored. Therefore, no new connection will
        be open
        """

        """
        Create connection and cursor for sqlite database
        """
        if db_cur and db_con:
            self.con = db_con
            self.cur = db_cur
        elif sql_db:
            self.sql_path = sql_db
            self.con = sqlite3.connect(sql_db, check_same_thread=False)
            self.cur = self.con.cursor()
            self.cur.execute("PRAGMA synchronous = OFF")
            # self.cur.execute("PRAGMA journal_mode = TRUNCATED")

        """
        Stores the "active" Alignment objects for the current AlignmentList.
        Keys will be the Alignment.path for quick lookup of Alignment object
        values
        """
        self.alignments = OrderedDict()

        """
        Stores the "inactive" or "shelved" Alignment objects. All AlignmentList
        methods will operate only on the alignments attribute, unless explicitly
        stated otherwise. Key-value is the same as self.alignments
        """
        self.shelve_alignments = OrderedDict()

        """
        Attribute list that stores the Alignment.name attribute of badly
        formatted alignments
        """
        self.bad_alignments = []

        """
        Attribute list that stores duplicate Alignment.name.
        """
        self.duplicate_alignments = []

        """
        Attribute list that stores sequence sets of unequal lenght
        """
        self.non_alignments = []

        """
        List with the name of the taxa included in the AlignmentList object
        """
        self.taxa_names = []

        """
        List with non active taxa
        """
        self.shelved_taxa = []

        self.path_list = []

        """
        Records the number of filtered alignments by each individual filter type
        """
        self.filtered_alignments = OrderedDict([("By minimum taxa", None),
                                               ("By taxa", None),
                                               ("By variable sites", None),
                                               ("By informative sites", None)])

        """
        Tuple with the AlignmentList sequence code. Either ("DNA", "n") or
        ("Protein", "x")
        """
        self.sequence_code = None
        self.gap_symbol = "-"

        """
        Dictionary with summary statistics for the active alignments
        """
        self.summary_stats = {"genes": 0, "taxa": 0, "seq_len": 0, "gaps": 0,
                              "avg_gaps": [], "missing": 0, "avg_missing": [],
                              "variable": 0, "avg_var": [], "informative": 0,
                              "avg_inf": []}

        """
        Dictionary with summary statistics for each alignment.
        """
        columns = ["genes", "nsites", "taxa", "var", "inf", "gap", "missing"]
        self.summary_gene_table = pd.DataFrame(columns=columns)

        """
        Lists the currently active tables. This is mainly used for the
        conversion of the consensus alignments into a single Alignment object
        """
        self.active_tables = []

        self.dest = dest
        self.pw_data = None

        # Set partitions object
        self.partitions = Partitions()

        # if type(alignment_list[0]) is str:
        if alignment_list:

            self.add_alignment_files(alignment_list, pbar=pbar)

    def __iter__(self):
        """
        Iterate over Alignment objects
        """
        return iter(self.alignments.values())

    def _create_table(self, table_name, index=None, cur=None):

        if not cur:
            cur = self.cur

        cur.execute("CREATE TABLE [{}]("
                    "txId INT,"
                    "taxon TEXT,"
                    "seq TEXT)".format(table_name))

        if index:
            cur.execute("CREATE INDEX {} ON [{}]({})".format(
                index[0], table_name, index[1]))

    def _table_exists(self, table_name, cur=None):

        if not cur:
            cur = self.cur

        return cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND"
            " name='{}'".format(table_name)).fetchall()

    def _set_pipes(self, ns=None, pbar=None, total=None, msg=None):

        if ns:
            if ns.stop:
                raise KillByUser("")

            if len(self.alignments) == 1:
                ns.sa = True
            else:
                ns.sa = False
                ns.total = total
                ns.counter = 0
        if pbar:
            pbar.max_value = total

    @staticmethod
    def _update_pipes(ns=None, pbar=None, value=None, msg=None):

        if ns:
            if ns.stop:
                raise KillByUser("")
            if not ns.sa:
                ns.msg = msg
                ns.counter = value
        if pbar:
            pbar.update(value)

    @staticmethod
    def _reset_pipes(ns):

        if ns:
            if ns.stop:
                raise KillByUser("")
            ns.counter = ns.total = ns.msg = ns.sa = None

    def aln_names(self):
        """
        :return: List with basenames of alignment file paths
        """

        return sorted([basename(x) for x in self.alignments])

    def resume_database(self):

        self.con = sqlite3.connect(self.sql_path, check_same_thread=False)
        self.cur = self.con.cursor()

        for aln in self.alignments.values() + self.shelve_alignments.values():
            aln.cur = self.cur
            aln.con = self.con

    def set_database_connections(self, cur, con):
        """
        Sets the database connections manually for the AlignmentList object
        and for each Alignment object
        :param cur:  Cursor object
        :param con:  Connection object
        """

        self.cur = cur
        self.con = con

        for aln in self.alignments.values() + self.shelve_alignments.values():
            aln.cur = cur
            aln.con = con

    def get_tables(self):
        """
        Return a list with the main table names of the Alignment objects
        """

        return [x.table_name for x in
                self.alignments.values() + self.shelve_alignments.values()]

    def remove_tables(self, preserve_tables=None, trash_tables=None):
        """
        Drops ALL tables from the database, except the ones specified via
        the preserve_tables argument
        :param preserve_tables: list. The tables that will NOT be dropped
        :param trash_tables: list. Only the tables that will be dropped.
        Takes precedence over preserver_tables
        """

        if not preserve_tables:
            preserve_tables = []

        if not trash_tables:
            trash_tables = []

        tables = self.cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table';").fetchall()

        if trash_tables:
            tables_delete = [x[0] for x in tables if x[0] in trash_tables]
        else:
            tables_delete = [x[0] for x in tables if x[0] not in
                             preserve_tables]

        for tb in tables_delete:
            self.cur.execute("DROP TABLE [{}]".format(tb))

    def clear_alignments(self):
        """
        Clears the current AlignmentList object
        :return:
        """

        # Drop all database tables related to the current Alignments
        for aln in self.alignments.values() + self.shelve_alignments.values():
            aln.remove_alignment()

        # Drop active databases of the AlignmentList instance, namely consensus
        # and concatenation, if they exist
        for table in self.active_tables:
            self.cur.execute("DROP TABLE [{}]".format(table))

        self.alignments = {}
        self.shelve_alignments = {}
        self.bad_alignments = []
        self.duplicate_alignments = []
        self.non_alignments = []
        self.taxa_names = []
        self.shelved_taxa = []
        self.path_list = []
        self.filtered_alignments = OrderedDict([("By minimum taxa", None),
                                                ("By taxa", None),
                                                ("By variable sites", None),
                                                ("By informative sites", None)])
        self.sequence_code = None
        self.summary_stats = {"genes": 0, "taxa": 0, "seq_len": 0, "gaps": 0,
                              "avg_gaps": [], "missing": 0, "avg_missing": [],
                              "variable": 0, "avg_var": [], "informative": 0,
                              "avg_inf": []}
        columns = ["genes", "nsites", "taxa", "var", "inf", "gap", "missing"]
        self.summary_gene_table = pd.DataFrame(columns=columns)
        self.active_tables = []
        self.dest = None
        self.pw_data = None
        self.partitions = Partitions()

    def _reset_summary_stats(self):

        self.summary_stats = {"genes": 0, "taxa": 0, "seq_len": 0, "gaps": 0,
                              "avg_gaps": [], "missing": 0, "avg_missing": [],
                              "variable": 0, "avg_var": [], "informative": 0,
                              "avg_inf": []}

    def update_active_alignments(self, aln_list=None, all_files=False):
        """
        Updates the self.alignments and self.shelve_alignments attributes.
        The Alignment.name's provided by the argument will populate
        self.alignments and the remaining will be
        """

        if all_files:
            for aln in self.shelve_alignments:
                self.alignments[aln] = self.shelve_alignments[aln]

        elif aln_list is not None:
            for aln in self.alignments.keys() + self.shelve_alignments.keys():

                if aln not in aln_list:
                    try:
                        self.shelve_alignments[aln] = self.alignments[aln]
                        del self.alignments[aln]
                    except KeyError:
                        pass
                else:
                    try:
                        self.alignments[aln] = self.shelve_alignments[aln]
                        del self.shelve_alignments[aln]
                    except KeyError:
                        pass

        # Update taxa names
        self.taxa_names = self._get_taxa_list()

    def update_active_alignment(self, aln_name, direction):
        """
        Same as update_active_alignments but for a single aln_name, so that
        the whole list does not need to be iterated
        :param aln_name: string, name of the alignment to move
        :param direction: string, can be either 'shelve' or 'active'
        """

        if direction == "shelve":
            self.shelve_alignments[aln_name] = self.alignments[aln_name]
            del self.alignments[aln_name]

        else:
            self.alignments[aln_name] = self.shelve_alignments[aln_name]
            del self.shelve_alignments[aln_name]

        # Update taxa names
        self.taxa_names = self._get_taxa_list()

    def update_taxa_names(self, taxa_list=None, all_taxa=False):
        """
        Shelves all taxa names, except those specified in taxa_list. This
        method should only be used when performing Process tasks, and was
        designed so that changes in the active taxa set are reversible
        :param taxa_list: list. List of active taxa
        :param all_taxa: boolean. If True, activates all taxa
        """

        # Activate all taxa
        if all_taxa:
            for tx in self.shelved_taxa:
                self.taxa_names.append(tx)

            self.shelved_taxa = []

        # Activate only taxa specified by taxa_list
        elif taxa_list:

            for tx in self.taxa_names + self.shelved_taxa:
                if tx not in taxa_list and tx in self.taxa_names:
                    try:
                        self.taxa_names.remove(tx)
                        self.shelved_taxa.append(tx)
                    except ValueError:
                        pass

                elif tx in taxa_list and tx in self.shelved_taxa:
                    try:
                        self.taxa_names.append(tx)
                        self.shelved_taxa.remove(tx)
                    except ValueError:
                        pass

        # Update individual Alignment objects
        for aln_obj in self.alignments.values():
            aln_obj.shelve_taxa(self.shelved_taxa)

    def format_list(self):
        """
        :return: List with the unique sequence types of the Alignment objects
        """

        return list(set([x.sequence_code[0] for x in
                         self.alignments.values() if x]))

    def _get_taxa_list(self, only_active=False):
        """
        Gets the full taxa list of all alignment objects
        :return full_taxa. List of taxa names in the AlignmentList
        """

        if only_active:
            full_taxa = list(set().union(*[x.taxa_list for x in
                                           self.alignments.values()]))
        else:
            full_taxa = list(set().union(*[x.taxa_list for x in
                                           self.alignments.values() +
                                           self.shelve_alignments.values()]))

        return full_taxa

    def _get_filename_list(self):
        """
        Returns a list with the input file names
        """
        return (alignment.name for alignment in self.alignments.values())

    def set_partition_from_alignment(self, alignment_obj):
        """
        Updates the partition object with the provided alignment_obj
        :param alignment_obj: Alignment object
        :return:
        """

        # Update partitions object
        # NOTE: The use_counter argument is set to False here so that when
        # the locus_range is provided, the counter does not interfere with the
        # ranges.
        if not alignment_obj.partitions.is_single():
            for k, v in alignment_obj.partitions:
                self.partitions.add_partition(
                    k, locus_range=v[0], codon=v[1],
                    use_counter=True, file_name=alignment_obj.path,
                    model_cls=alignment_obj.partitions.models[k])
        else:
            self.partitions.add_partition(
                alignment_obj.name, use_counter=True,
                file_name=alignment_obj.path,
                length=alignment_obj.locus_length,
                model_cls=alignment_obj.partitions.models[alignment_obj.name])

    def add_alignments(self, alignment_obj_list, ignore_paths=False):
        """
        Adds a new Alignment object
        :param alignment_obj_list: list with Alignment objects
        """

        for alignment_obj in alignment_obj_list:

            # Check for badly formatted alignments
            if isinstance(alignment_obj.e, InputError):
                self.bad_alignments.append(alignment_obj.path)
            elif isinstance(alignment_obj.e,
                            AlignmentUnequalLength):
                self.non_alignments.append(alignment_obj.path)

            # if not ignore_paths:
            if not ignore_paths:
                if alignment_obj.path in [x.path for x in
                                          self.alignments.values()]:
                    self.duplicate_alignments.append(alignment_obj.name)
                else:
                    # Get seq code
                    if not self.sequence_code:
                        self.sequence_code = alignment_obj.sequence_code

                    self.alignments[alignment_obj.path] = alignment_obj
                    self.set_partition_from_alignment(alignment_obj)
            else:
                # Get seq code
                if not self.sequence_code:
                    self.sequence_code = alignment_obj.sequence_code

                self.alignments[alignment_obj.name] = alignment_obj
                self.set_partition_from_alignment(alignment_obj)
                # self.filename_list.append(alignment_obj.name)

        self.taxa_names = self._get_taxa_list()

    def add_alignment_files(self, file_name_list, pbar=None,
                            shared_namespace=None):
        """
        Adds a new alignment based on a file name
        :param file_name_list: list, with the path to the alignment files
        """

        # Check for duplicates among current file list
        for f in [x for x, y in Counter(file_name_list).items() if y > 1]:
            self.duplicate_alignments.append(f)
            file_name_list.remove(f)

        # Check for duplicates between current file list and previous file list
        for i in set(self.path_list).intersection(set(file_name_list)):
            self.duplicate_alignments.append(i)
            file_name_list.remove(i)

        if shared_namespace:
            shared_namespace.progress = 0

        if pbar:
            pbar.max_value = len(file_name_list)

        for p, aln_path in enumerate(file_name_list):

            # Progress bar update for command line version
            if pbar:
                pbar.update(p + 1)

            if shared_namespace:
                shared_namespace.progress += 1
                shared_namespace.m = "Processing file {}".format(
                    basename(aln_path))

                if shared_namespace.stop:
                    raise KillByUser("Child thread killed by user")

            aln_obj = Alignment(aln_path, sql_cursor=self.cur,
                                sql_con=self.con)

            if isinstance(aln_obj.e, InputError):
                self.bad_alignments.append(aln_obj.path)
            elif isinstance(aln_obj.e, AlignmentUnequalLength):
                self.non_alignments.append(aln_obj.path)
            elif isinstance(aln_obj.e, EmptyAlignment):
                self.bad_alignments.append(aln_obj.path)
            else:
                # Get seq code
                if not self.sequence_code:
                    self.sequence_code = aln_obj.sequence_code
                # Check for multiple sequence types. If True,
                # raise Exception
                elif self.sequence_code[0] != aln_obj.sequence_code[0]:
                    raise MultipleSequenceTypes("Multiple sequence "
                        "types detected: {} and {}".format(
                            self.sequence_code[0],
                            aln_obj.sequence_code[0]))

                self.alignments[aln_obj.path] = aln_obj
                self.set_partition_from_alignment(aln_obj)
                self.path_list.append(aln_obj.path)

        if self.alignments:
            self.taxa_names = self._get_taxa_list()

    def retrieve_alignment(self, name):
        """
        :param name: string. Name of the input alignment
        :return: Returns an Alignment object with a given name attribute
        """

        if name in self.alignments:
            return self.alignments[name]
        elif name in self.shelve_alignments:
            return self.shelve_alignments[name]
        else:
            return None

    def iter_alignment_files(self):
        """
        :return: Iterable with the file names for each Alignment object
        """

        return iter(alignment.path for alignment in self.alignments.values())

    def write_taxa_to_file(self):
        """
        Compiles the taxa names of all alignments and writes them in a single
        column .csv file
        """

        output_handle = open("Taxa_list.csv", "w")

        for taxon in self.taxa_names:
            output_handle.write(taxon + "\n")

        output_handle.close()

    def concatenate(self, alignment_name=None, table_in="", ns=None,
                    pbar=None):
        """
        Concatenates multiple sequence alignments creating a single alignment
        object and the auxiliary Partitions object defining the partitions
        of the concatenated alignment
        
        Rationale behind the concatenation procedure: 
          Since sqlite queries are quite expensive, the previous approach
          of querying the sequence for each taxa AND alignment table was 
          dropped. In this approach, sqlite will do all the heavy lifting.
          First, a temporary table with the same definition as all alignment
          tables and populated with data from all taxa and alignments. It is
          important that and index is created on the new txId column, which
          will redifine the txId of individual alignments according to the 
          global self.tax_names attribute. In the first procedure, there 
          will be only one query per alignment. When the temporary table is
          complete, some sql magic is used to group sequences from each
          taxon for all alignments and then concatenated them, all in a single
          query. This query returns the concatenated sequences, corrected txId
          and taxon information, ready to populate the final concatenation
          table. This approach is an order o magnitude faster than the
          previous one where thousands of queries could be performed.
        
        
        :param alignment_name: string. Optional. Name of the new concatenated
        alignment object. This should be used when collapsing the alignment
        afterwards.
        :return concatenated_alignment: Alignment object
        """

        self._set_pipes(ns, pbar, total=len(self.alignments))

        # Create table temporary table and create an index
        temp_table = "concatenation_temp"
        self._create_table(temp_table, index=("concidex", "txId"))

        # Variables that will store the taxa_list and taxa_idx that will be
        # provided when instantiating an Alignment object
        taxa_list = self.taxa_names
        taxa_idx = dict((tx, idx) for idx, tx in enumerate(self.taxa_names))

        # Create new cursor to insert data into database while the main
        # cursor returns the query
        conc_cur = self.con.cursor()

        for p, aln in enumerate(self.alignments.values()):

            # Update progress information
            self._update_pipes(ns, pbar, value=p + 1,
                               msg="Processing alignment {}".format(aln.name))

            # For each taxon present in the current Alignment object,
            # update the txId in the new table using the taxa_idx local
            # variable.
            for tx, seq in aln.iter_alignment(table_suffix=table_in):

                conc_cur.execute("INSERT INTO [{}] VALUES (?, ?, ?)".format(
                    temp_table), (taxa_idx[tx], tx, seq))

            # Get all the taxa missing from the current Alignment object
            missing_tx = set(self.taxa_names) - set(aln.taxa_list)

            # Insert missing data for the missing taxa in the teporary database
            for tx in missing_tx:

                conc_cur.execute("INSERT INTO [{}] VALUES (?, ?, ?)".format(
                    temp_table),
                    (taxa_idx[tx], tx, aln.sequence_code[1] * aln.locus_length))

        # Set the name of the final concatenated table
        table = "concatenation"

        # Create the table
        self._create_table(table)
        self.active_tables.append(table)

        # Reset progress information for next loop
        self._reset_pipes(ns)
        self._set_pipes(ns, pbar, total=len(self.taxa_names))

        # Here is where sqlite will do the heavy lifting of grouping sequences
        # from the same taxon across all alignments and then concatenating
        # them. It is important that the column that will be grouped has
        # an index (for perfomance and memory reasons). While seqs are
        # grouped by txId, the GROUP_CONCAT() method is used to return
        # concatenated strings.
        for p, (idx, tx, seq) in enumerate(self.cur.execute(
            "SELECT txId, taxon, GROUP_CONCAT(seq, '') FROM [{}] "
            "GROUP BY txId".format(temp_table))):

            self._update_pipes(ns, pbar, value=p + 1,
                               msg="Concatenating taxon {}".format(tx))

            conc_cur.execute("INSERT INTO [{}] VALUES (?, ?, ?)".format(
                table), (idx, tx, seq))

        # Variable that will store the length of the concatenated alignment
        # and provided it when initializing the Alignment object
        locus_length = len(seq)

        # DROP the temporary table
        self.cur.execute("DROP TABLE [{}]".format(temp_table))

        # Removes partitions that are currently in the shelve
        for aln_obj in self.shelve_alignments.values():
            try:
                self.partitions.remove_partition(file_name=aln_obj.path)
            except PartitionException:
                pass

        # Create the concatenated file in an Alignment object
        concatenated_alignment = Alignment(table,
                                           partitions=self.partitions,
                                           locus_length=locus_length,
                                           sql_cursor=self.cur,
                                           sql_con=self.con,
                                           sequence_code=self.sequence_code,
                                           taxa_list=taxa_list,
                                           taxa_idx=taxa_idx)

        return concatenated_alignment

    def filter_min_taxa(self, min_taxa, ns=None, pbar=None):
        """
        Filters Alignment objects based on a minimum taxa representation
        threshold. Alignments with less that the specified minimum taxa
        percentage will be moved to the filtered_alignments attribute.

        NOTE: Since this filtering is meant to be performed when executing
        the process operations it will permanently change the AlignmentList
        object, which means both self.alignments and self.partitions. Not doing
        so and removing/adding the partitions would create a great deal of
        conflicts that can be easily avoided by simply copying the
        AlignmentList object and modifying this object for the process execution

        :param min_taxa: integer, percentage of minimum taxa below which
        alignments are moved to the filtered_alignments attribute
        """

        self._set_pipes(ns, pbar, total=len(self.alignments))

        self.filtered_alignments["By minimum taxa"] = 0

        for p, (k, alignment_obj) in enumerate(list(self.alignments.items())):

            self._update_pipes(ns, pbar, value=p + 1,
                               msg="Evaluating file {}".format(
                    basename(alignment_obj.name)))

            if len(alignment_obj.taxa_list) < \
                    (float(min_taxa) / 100.) * len(self.taxa_names):
                self.update_active_alignment(k, "shelve")
                self.partitions.remove_partition(file_name=alignment_obj.path)
                self.filtered_alignments["By minimum taxa"] += 1

        self._reset_pipes(ns)

    def filter_by_taxa(self, filter_mode, taxa_list, ns=None, pbar=None):
        """
        Filters the alignments attribute by taxa list. The filtering may be done
        to exclude or include a particular set of taxa
        :param filter_mode: string, determines the filtering mode. Can be either
        'Contain' or 'Exclude'
        :param taxa_list: list, contains the list of taxa to be used for
        filtering
        """

        self._set_pipes(ns, pbar, total=len(self.alignments))

        self.filtered_alignments["By taxa"] = 0

        # Support automatic file detection if taxa_list is a string
        if isinstance(taxa_list, str):
            try:
                file_handle = open(taxa_list)
                taxa_list = self.read_basic_csv(file_handle)
            except IOError:
                pass
        elif len(taxa_list) == 1:
            try:
                file_handle = open(taxa_list[0])
                taxa_list = self.read_basic_csv(file_handle)
            except IOError:
                pass

        for p, (k, alignment_obj) in enumerate(list(self.alignments.items())):

            self._update_pipes(ns, pbar, value=p + 1,
                               msg="Filtering file {}".format(
                    basename(alignment_obj.name)))

            # Filter alignments that do not contain at least all taxa in
            # taxa_list
            if filter_mode == "Contain":
                if set(taxa_list) - set(alignment_obj.taxa_list) != set():
                    self.update_active_alignment(k, "shelve")
                    self.partitions.remove_partition(
                        file_name=alignment_obj.path)
                    self.filtered_alignments["By taxa"] += 1

            # Filter alignments that contain the taxa in taxa list
            if filter_mode == "Exclude":
                if any((x for x in taxa_list
                        if x in alignment_obj.taxa_list)):
                    self.update_active_alignment(k, "shelve")
                    self.partitions.remove_partition(
                        file_name=alignment_obj.path)
                    self.filtered_alignments["By taxa"] += 1

        self.taxa_names = self._get_taxa_list(only_active=True)

        # If the resulting alignment is empty, raise an Exception
        if self.alignments == {}:
            if ns:
                ns.exception = "EmptyAlignment"
            raise EmptyAlignment("Alignment is empty after taxa filter")

        self._reset_pipes(ns)

    def filter_codon_positions(self, *args, **kwargs):
        """
        Filter codon positions from DNA alignments.
        :param position_list: list containing a boolean value for each codon
        position. Ex. [True, True, True] will save all positions while
        [True, True, False] will exclude the third codon position
        :param table_in: string. Name of the table from where the sequence data
        will be retrieved. This will be determined from the SetupDatabase
        decorator depending on whether the table_out table already exists
        in the sqlite database. Leave None to use the main Alignment table
        :param table_out: string. Name of the table that will be
        created/modified in the database to harbor the collapsed alignment
        """

        ns = kwargs.get("ns", None)
        pbar = kwargs.pop("pbar", None)

        self._set_pipes(ns, pbar, total=len(self.alignments))

        # Reset partitions
        self.partitions = Partitions()

        for p, alignment_obj in enumerate(list(self.alignments.values())):

            self._update_pipes(ns, pbar, value=p + 1,
                               msg="Filtering file {}".format(
                    basename(alignment_obj.name)))

            alignment_obj.filter_codon_positions(*args, **kwargs)

            self.set_partition_from_alignment(alignment_obj)

        self._reset_pipes(ns)

    def filter_missing_data(self, *args, **kwargs):
        """
        Wrapper of the filter_missing_data method of the Alignment object.
        See the method's documentation.
        :param gap_threshold: integer, percentage of gap symbols below which
        the alignment column should be filtered
        :param missing_threshold: integer, percentage of missing data (gaps +
        true missing data) below which the alignment column should be fitered
        :param table_in: string. Name of the table from where the sequence data
        will be retrieved. This will be determined from the SetupDatabase
        decorator depending on whether the table_out table already exists
        in the sqlite database. Leave None to use the main Alignment table
        :param table_out: string. Name of the table that will be
        created/modified in the database to harbor the collapsed alignment
        """

        ns = kwargs.get("ns", None)
        pbar = kwargs.pop("pbar", None)

        # Reset partitions
        self.partitions = Partitions()

        self._set_pipes(ns, pbar, total=len(self.alignments))

        for p, alignment_obj in enumerate(list(self.alignments.values())):

            self._update_pipes(ns, pbar, value=p + 1,
                               msg="Filtering file {}".format(
                        basename(alignment_obj.name)))

            alignment_obj.filter_missing_data(*args, **kwargs)

            self.set_partition_from_alignment(alignment_obj)

        self._reset_pipes(ns)

    def filter_segregating_sites(self, *args, **kwargs):
        """
        Wrapper of the filter_segregating_sites method of the Alignment
        object. See the method's documentation
        :param min_val: Integer. If not None, sets the minimum number of
        segregating sites allowed for the alignment to pass the filter
        :param max_val: Integer. If not None, sets the maximum number of
        segregating sites allowed for the alignment to pass the filter
        :param table_in: string. Name of the table from where the sequence data
        will be retrieved. This will be determined from the SetupDatabase
        decorator depending on whether the table_out table already exists
        in the sqlite database. Leave None to use the main Alignment table
        """

        ns = kwargs.get("ns", None)
        pbar = kwargs.pop("pbar", None)

        self._set_pipes(ns, pbar, total=len(self.alignments))

        self.filtered_alignments["By variable sites"] = 0

        for p, (k, alignment_obj) in enumerate(list(self.alignments.items())):

            self._update_pipes(ns, pbar, value=p + 1,
                               msg="Filtering file {}".format(
                    basename(alignment_obj.name)))

            if not alignment_obj.filter_segregating_sites(*args, **kwargs):
                self.update_active_alignment(k, "shelve")
                self.partitions.remove_partition(file_name=alignment_obj.path)
                self.filtered_alignments["By variable sites"] += 1

        self._reset_pipes(ns)

    def filter_informative_sites(self,*args, **kwargs):
        """
        Wrapper of the filter_informative_sites method of the Alignment
        object. See the method's documentation
        :param min_val: Integer. If not None, sets the minimum number of
        informative sites allowed for the alignment to pass the filter
        :param max_val: Integer. If not None, sets the maximum number of
        informative sites allowed for the alignment to pass the filter
        """

        ns = kwargs.get("ns", None)
        pbar = kwargs.pop("pbar", None)

        self._set_pipes(ns, pbar, total=len(self.alignments))

        self.filtered_alignments["By informative sites"] = 0

        for p, (k, alignment_obj) in enumerate(list(self.alignments.items())):

            self._update_pipes(ns, pbar, value=p + 1,
                               msg="Filtering file {}".format(
                    basename(alignment_obj.name)))

            if not alignment_obj.filter_informative_sites(*args, **kwargs):
                self.update_active_alignment(k, "shelve")
                self.partitions.remove_partition(file_name=alignment_obj.path)
                self.filtered_alignments["By informative sites"] += 1

        self._reset_pipes(ns)

    def remove_taxa(self, taxa_list, mode="remove"):
        """
        Wrapper of the remove_taxa method of the Alignment object for
        multiple alignments. It current supports two modes:

            ..:remove: removes specified taxa
            ..:inverse: removes all but the specified taxa
        """

        # Checking if taxa_list is an input csv file:
        try:
            file_handle = open(taxa_list[0])
            taxa_list = self.read_basic_csv(file_handle)

        # If not, then the method's argument is already the final list
        except (IOError, IndexError):
            pass

        for alignment_obj in list(self.alignments.values()):
            alignment_obj.remove_taxa(taxa_list, mode=mode)

        # Updates taxa names
        if mode == "remove":
            for tx in taxa_list:
                try:
                    self.taxa_names.remove(tx)
                except ValueError:
                    # TODO: log a warning
                    pass
        elif mode == "inverse":
            self.taxa_names = [tx for tx in taxa_list if tx in self.taxa_names]

    def change_taxon_name(self, old_name, new_name):
        """ Changes the name of a taxon. """

        for alignment_obj in list(self.alignments.values()):
            alignment_obj.change_taxon_name(old_name, new_name)

        # update taxa names
        self.taxa_names = [new_name if x == old_name else x
                           for x in self.taxa_names]

    def remove_file(self, filename_list):
        """
        Removes alignment objects based on their name attribute
        :param filename_list: list with the names of the alignment objects to
        be removed
        """

        # If filename_list corresponds to all files in the current alignment
        # list, dispatch the clear_alignments methods
        if set(self.path_list) - set(filename_list) == set([]):
            self.clear_alignments()

        for nm in filename_list:
            if nm in self.alignments:
                self.alignments[nm].remove_alignment()
                del self.alignments[nm]
            elif nm in self.shelve_alignments:
                self.shelve_alignments[nm].remove_alignment()
                del self.shelve_alignments[nm]
            try:
                self.partitions.remove_partition(file_name=nm)
            except PartitionException:
                pass

        # Updates taxa names
        self.taxa_names = self._get_taxa_list()

    # def shelve_file(self, filename_list):
    #     """
    #     Instead of completely removing the Alignment object, these are moved
    #     to the shelve_alignments list.
    #     :param filename_list: list with the names of the alignment objects to
    #     be removed
    #     """
    #
    #     for nm in filename_list:
    #         nm_wext = basename(nm)
    #         nm = basename(nm).split(".")[0]
    #         if nm in self.alignments:
    #             self.shelve_alignments[nm] = self.alignments[nm]
    #             del self.alignments[nm]
    #         self.partitions.remove_partition(file_name=nm_wext)
    #
    #     # Updates taxa names
    #     self.taxa_names = self._get_taxa_list()

    def select_by_taxa(self, taxa_list, mode="strict"):
        """
        This method is used to selected gene alignments according to a list
        of taxa.

        :param taxa_list. List of taxa names

        :param mode. String. Modes can be the following
            ..:strict: The taxa of the alignment must be exactly the same as the
        specified taxa.
            ..:inclusive: The taxa of the alignment must contain all specified
        taxa.
            ..:relaxed: At least on of the specified taxa must be in the taxa of
        the alignment.
        """

        selected_alignments = []

        # taxa_list may be a file name (string) or a list containing the name
        # of the taxa. If taxa_list is a file name this code will parse the
        # csv file and return a list of the taxa. Otherwise, the taxa_list
        # variable remains the same.
        try:
            file_handle = open("".join(taxa_list))
            taxa_list = self.read_basic_csv(file_handle)
        except EnvironmentError:
            pass

        for alignment_obj in self.alignments.values():

            alignment_taxa = alignment_obj.taxa_list

            # Selected only the alignments with the exact same taxa
            if mode == "strict":
                if set(taxa_list) == set(alignment_taxa):
                    selected_alignments.append(alignment_obj)

            # Selected alignments that include the specified taxa
            if mode == "inclusive":
                if set(taxa_list) - set(alignment_taxa) == set():
                    selected_alignments.append(alignment_obj)

            if mode == "relaxed":
                for taxon in taxa_list:
                    if taxon in alignment_taxa:
                        selected_alignments.append(alignment_obj)
                        continue

        return selected_alignments

    def code_gaps(self, *args, **kwargs):
        """
        Wrapper for the code_gaps method of the Alignment object.
        """

        ns = kwargs.get("ns", None)
        pbar = kwargs.pop("pbar", None)

        self._set_pipes(ns, pbar, total=len(self.alignments))

        for p, alignment_obj in enumerate(self.alignments.values()):

            self._update_pipes(ns, pbar, value=p + 1,
                               msg="Coding file {}".format(
                        basename(alignment_obj.name)))

            alignment_obj.code_gaps(*args, **kwargs)

        self._reset_pipes(ns)

    def collapse(self, *args, **kwargs):
        """
        Wrapper for the collapse method of the Alignment object. If
        write_haplotypes is True, the haplotypes file name will be based on the
        individual input file
        """

        ns = kwargs.get("ns", None)
        pbar = kwargs.get("pbar", None)

        write_haplotypes = kwargs.get("write_haplotypes", None)
        conversion_suffix = kwargs.get("conversion_suffix", None)
        haplotypes_file = kwargs.get("haplotypes_file", None)

        self._set_pipes(ns, pbar, total=len(self.alignments))

        for p, alignment_obj in enumerate(self.alignments.values()):

            self._update_pipes(ns, pbar, value=p + 1,
                               msg="Collapsing file {}".format(
                        basename(alignment_obj.name)))

            if write_haplotypes:
                # Set name for haplotypes file
                output_file = alignment_obj.name.split(".")[0] + \
                              conversion_suffix + haplotypes_file
                alignment_obj.collapse(haplotypes_file=output_file, *args,
                                       **kwargs)
            else:
                alignment_obj.collapse(*args, **kwargs)

        self._reset_pipes(ns)

    def consensus(self, *args, **kwargs):
        """
        If single_file is set to False, this acts as a simple wrapper to
        to the consensus method of the Alignment object.
        If single_file is True, then a new table is created and populated here
        for the new alignment object akin to the concatenation one. That
        Alignment object is then returned.
        """

        ns = kwargs.get("ns", None)
        pbar = kwargs.get("pbar", None)
        single_file = kwargs.pop("single_file", None)

        self._set_pipes(ns, pbar, len(self.alignments))

        self.taxa_names = ["consensus"]

        # Variables that will store the taxa_list and taxa_idx to provide
        # to the Alignment object
        taxa_list = []
        taxa_idx = {}

        if single_file:
            # Create a table that will harbor the consensus sequences of all
            # Alignment objects
            self._create_table("consensus")
            self.active_tables.append("consensus")
            consensus_data = []

        for p, alignment_obj in enumerate(self.alignments.values()):

            self._update_pipes(ns, pbar, value=p + 1,
                               msg="Processing file {}".format(
                        basename(alignment_obj.name)))

            alignment_obj.consensus(*args, **kwargs)

            if single_file:
                table_out = kwargs.get("table_out", None)
                sequence = alignment_obj.get_sequence("consensus",
                                                      table_suffix=table_out)
                consensus_data.append((p, alignment_obj.sname, sequence))
                taxa_list.append(alignment_obj.sname)
                taxa_idx[alignment_obj.sname] = p

        self._reset_pipes(ns)

        if single_file:

            use_main_table = kwargs.get("use_main_table", None)
            if use_main_table:
                self.remove_tables(preserve_tables=["consensus"])

            # Populate database table
            self.cur.executemany("INSERT INTO [{}] VALUES (?, ?, ?)".format(
                "consensus"), consensus_data)

            # Create Alignment object
            consensus_aln = Alignment("consensus", sql_cursor=self.cur,
                                      sql_con=self.con,
                                      sequence_code=self.sequence_code,
                                      taxa_list=taxa_list,
                                      taxa_idx=taxa_idx)

            return consensus_aln

    def reverse_concatenate(self, ns=None):
        """
        Internal function to reverse concatenate an alignment according to
        defined partitions in a Partitions object

        This will only work if alignment_object_list has one alignment, as it
        is intended to be a wrapper of sorts for the Alignment object method

        :return: AlignmentList object with individual alignments
        """

        concatenated_aln = self.concatenate(alignment_name="concatenated")

        reverted_alns = concatenated_aln.reverse_concatenate(db_con=self.con,
                                                             ns=ns)

        return reverted_alns

    def write_to_file(self, output_format, conversion_suffix="",
                      output_suffix="", *args, **kwargs):
        """
        Wrapper of the write_to_file method of the Alignment object for multiple
        alignments.
        """

        ns_pipe = kwargs.get("ns_pipe", None)
        pbar = kwargs.pop("pbar", None)

        self._set_pipes(ns_pipe, pbar, len(self.alignments))

        for p, alignment_obj in enumerate(self.alignments.values()):

            self._update_pipes(ns_pipe, pbar, value=p + 1,
                               msg="Writting file {}".format(
                                   basename(alignment_obj.name)))

            output_file_name = alignment_obj.name.split(".")[0] + \
                conversion_suffix + output_suffix

            kwargs["output_file"] = output_file_name

            # Get partition name for current alignment object
            try:
                part_name = [x for x, y in
                             self.partitions.partitions_alignments.items() if
                             alignment_obj.path in y][0]
            except IndexError:
                try:
                    part_name = [x for x, y in
                                 self.partitions.partitions_alignments.items()
                                 if alignment_obj.name in y][0]
                except IndexError:
                    part_name = None

            # Get model from partitions
            if part_name:
                m = self.partitions.models[part_name]
                alignment_obj.partitions.set_model(part_name, m[1])

            alignment_obj.write_to_file(output_format, *args, **kwargs)

        self._reset_pipes(ns_pipe)

    def get_gene_table_stats(self, active_alignments=None, sortby=None,
                             ascending=True):
        """
        Gets summary statistics for each individual gene for the gene table
        view. Summary statistics have already been calculated in
        get_summary_stats, so here we only get the active alignments for
        showing. Therefore, this function should only be called after
        get_summary_stats.
        :param active_alignments: List, containing the names of the active
        alignments from which he summary statistics will be retrieved
        """

        # Set table header
        table = [["Gene name", "Number of sites", "Number of taxa",
                  "Variable sites", "Informative sites", "Gaps",
                  "Missing data"]]
        # Table line keys matching summary_gene_table dict values
        tl = ["nsites", "taxa", "var", "inf", "gap", "missing"]

        # Update the active alignments
        if active_alignments and active_alignments != list(
                self.alignments.keys()):
            self.update_active_alignments(active_alignments)

        # Filter table with active alignments
        summary_gene_table = self.summary_gene_table[
            self.summary_gene_table["genes"].isin(self.aln_names())]

        if not sortby:
            sortby = ["genes"]

        summary_gene_table.sort_values(sortby, ascending=ascending,
                                       inplace=True)

        # Populate table information
        for k in self.summary_gene_table.itertuples():
            # Add table line
            table.append(list(k[1:]))

        return summary_gene_table, table

    # Stats methods
    def get_summary_stats(self, active_alignments=None, ns=None):
        """
        Creates/Updates summary statistics for the active alignments.
        :param active_alignments: List, containing names of the active
        alignments from which the summary statistics will be retrieved

        :param ns: Namepsace object. Used to communicate with the main thread
        """

        # Update active alignments if they changed since last update
        if active_alignments and \
                active_alignments != list(self.alignments.keys()):
            self.update_active_alignments(active_alignments)

        if ns:
            ns.files = len(self.alignments)

        # Set table header for summary_stats
        table = [["Genes", "Taxa", "Alignment length", "Gaps",
                  "Gaps per gene", "Missing data", "Missing data per gene",
                  "Variable sites", "Variable sites per gene",
                  "Informative sites", "Informative sites per gene"]]
        # Table line keys matching summary_stats for table completion
        tl = ["genes", "taxa", "seq_len", "gaps", "avg_gaps", "missing",
              "avg_missing", "variable", "avg_var", "informative", "avg_inf"]

        # Reset summary_stats
        self._reset_summary_stats()

        # Get number of alignments
        self.summary_stats["genes"] = len(self.alignments)

        # Get number of taxa
        self.summary_stats["taxa"] = len(self.taxa_names)

        # Get statistics that require iteration over alignments
        for p, (_, aln) in enumerate(sorted(self.alignments.items())):

            if ns:
                if ns.stop:
                    raise KillByUser("Child thread killed by user")
                ns.counter += 1

            # Get alignment size info
            self.summary_stats["seq_len"] += aln.locus_length

            cur_gap, cur_missing = 0, 0
            cur_var, cur_inf = 0, 0

            for col in aln.iter_columns():

                if ns:
                    if ns.stop:
                        raise KillByUser("")

                col = Counter(col)

                # Get missing data and gaps
                if self.sequence_code[1] in col:
                    self.summary_stats["missing"] += 1
                    cur_missing += 1
                if self.gap_symbol in col:
                    self.summary_stats["gaps"] += 1
                    cur_gap += 1

                # Get variability information
                # Filter missing data
                for i in [self.sequence_code[1], self.gap_symbol]:
                    del col[i]

                # If it's only missing data, ignore
                if col:
                    # Get variable sites
                    if len(col) > 1:
                        self.summary_stats["variable"] += 1
                        cur_var += 1

                    # If any of the remaining sites is present in more than two
                    # taxa score the site as informative
                    if len([x for x in col.values() if x >= 2]) >= 2:
                        self.summary_stats["informative"] += 1
                        cur_inf += 1

            # Get values for current alignment for average calculations
            self.summary_stats["avg_gaps"].append(cur_gap)
            self.summary_stats["avg_missing"].append(cur_missing)
            self.summary_stats["avg_var"].append(cur_var)
            self.summary_stats["avg_inf"].append(cur_inf)

            if not any(self.summary_gene_table["genes"] == aln.name):
                # Get row information for current gene in dict format
                row_info = (aln.name,
                            aln.locus_length,
                            len(aln.taxa_list),
                            cur_var,
                            cur_inf,
                            cur_gap,
                            cur_missing)
                # Create DataFrame from dict
                row_dt = pd.DataFrame([row_info], index=[p],
                                      columns=self.summary_gene_table.columns)
                # Add row DF to main DF
                self.summary_gene_table = pd.concat([self.summary_gene_table,
                                                     row_dt])

        # Get average values
        for k in ["avg_gaps", "avg_missing", "avg_var", "avg_inf"]:
            self.summary_stats[k] = round(np.mean(self.summary_stats[k]))

        # Get percentage values for missing data
        total = self.summary_stats["seq_len"] * self.summary_stats["taxa"]
        for k in ["gaps", "missing"]:
            n = self.summary_stats[k]
            percentage = round((float(n) / float(total)) * 100, 2)
            self.summary_stats[k] = "{0:,} ({1}%)".format(n, percentage)

        # Get percentage values for variation
        for k in ["variable", "informative"]:
            n = self.summary_stats[k]
            percentage = round((float(n) /
                                float(self.summary_stats["seq_len"])) * 100, 2)
            self.summary_stats[k] = "{0:,} ({1}%)".format(n, percentage)

        # Complete table information
        table.append([self.summary_stats[x] for x in tl])

        return dict(self.summary_stats), table

    @check_data
    def gene_occupancy(self, ns=None):
        """
        Creates data for an interpolation plot to visualize the amount of
        missing genes in a phylogenomics data set

        :param ns: Namespace object. Allows communication with main thread
        """

        data = []

        for alignment in self.alignments.values():
            if ns:
                if ns.stop:
                    raise KillByUser()

            data.append([1 if x in alignment.taxa_list else 0
                         for x in self.taxa_names])

        data = np.transpose(data)

        return {"data": data,
                "ax_names": ["Genes", "Taxa"],
                "title": "Gene occupancy"}

    @check_data
    def missing_data_distribution(self, ns=None):
        """
        Creates data for an overall distribution of missing data

        :param ns: Namespace object. Communicates with main thread
        """

        if ns:
            ns.files = len(self.alignments)

        legend = ["Gaps", "Missing data", "Data"]

        data_storage = OrderedDict((x, []) for x in legend)

        for aln in self.alignments.values():

            if ns:
                # Kill switch to interrupt worker
                if ns.stop:
                    raise KillByUser("")
                # Add to progress counter
                ns.counter += 1

            gaps_g, missing_g, data_g = [], [], []
            for tx in self.taxa_names:
                if tx in aln.taxa_list:
                    seq = aln.get_sequence(tx)
                    # Get gaps
                    gaps = float(seq.count("-"))
                    gaps_g.append(gaps / float(aln.locus_length))
                    # Get missing data
                    missing = float(seq.count(aln.sequence_code[1]))
                    missing_g.append(missing / float(aln.locus_length))
                    # Get actual data
                    actual_data = (float(aln.locus_length) - gaps - missing) / \
                        float(aln.locus_length)
                    data_g.append(actual_data)
                else:
                    gaps_g.append(0)
                    missing_g.append(1)
                    data_g.append(0)

            data_storage["Gaps"].append(np.mean(gaps_g))
            data_storage["Missing data"].append(np.mean(missing_g))
            data_storage["Data"].append(np.mean(data_g))

        data = [x for x in data_storage.values()]

        return {"data": data,
                "title": "Distribution of missing data",
                "legend": legend,
                "ax_names": ["Proportion", "Number of genes"],
                "table_header": ["Bin"] + legend}

    @check_data
    def missing_data_per_species(self, ns=None):
        """
        Creates data for a distribution of missing data per species

        :param ns: Namespace object. Communicates with main thread
        """

        # Data for a stacked bar plot. First element for gaps, second for
        # missing, third for actual data
        data_storage = OrderedDict((taxon, [0, 0, 0]) for taxon in
                                   self.taxa_names)
        total_len = 0

        legend = ["Gaps", "Missing", "Data"]

        if ns:
            ns.files = len(self.alignments)

        for aln in self.alignments.values():

            if ns:
                # Kill switch to interrupt worker
                if ns.stop:
                    raise KillByUser("")
                # Add to progress counter
                ns.counter += 1

            total_len += aln.locus_length
            for taxon in data_storage:
                if taxon in aln.taxa_list:
                    # Get gaps
                    seq = aln.get_sequence(taxon)
                    gaps = seq.count("-")
                    data_storage[taxon][0] += gaps
                    # Get missing
                    missing = seq.count(aln.sequence_code[1])
                    data_storage[taxon][1] += missing
                    # Get actual data
                    actual_data = aln.locus_length - gaps - missing
                    data_storage[taxon][2] += actual_data
                else:
                    data_storage[taxon][1] += aln.locus_length

        data_storage = OrderedDict(sorted(data_storage.items(),
                                          key=lambda x: x[1][1] + x[1][0],
                                          reverse=True))

        data = np.array([[float(x[0]) for x in
                          data_storage.values()],
                         [float(x[1]) for x in
                          data_storage.values()],
                         [float(x[2]) for x in
                          data_storage.values()]])

        return {"data": data,
                "title": "Distribution of missing data per species",
                "labels": list(data_storage.keys()),
                "legend": legend,
                "ax_names": [None, "Frequency"],
                "table_header": ["Taxon", "Gaps", "%", "Missing", "%", "Data",
                                 "%"],
                "normalize": True,
                "normalize_factor": total_len}

    @check_data
    def missing_genes_per_species(self, ns=None):
        """
        Creates data for the distribution of missing genes per species

        :param ns : Namespace object. Communicates with main thread
        :return: dictionary with arguments for plotting functions
        """

        data_storage = OrderedDict((taxon, 0) for taxon in self.taxa_names)

        if ns:
            ns.files = len(self.alignments)

        for aln in self.alignments.values():

            if ns:
                # Kill switch to interrupt worker
                if ns.stop:
                    raise KillByUser("")
                # Add to progress counter
                ns.counter += 1

            for key in data_storage:
                if key not in aln.taxa_list:
                    data_storage[key] += 1

        # Sort data in descending order of missing genes
        data_storage = OrderedDict(sorted(data_storage.items(), reverse=True,
                                          key=lambda t: t[1]))

        return {"data": [list(data_storage.values())],
                "labels": list(data_storage.keys()),
                "title": "Distribution of missing genes per species",
                "ax_names": [None, "Frequency"],
                "table_header": ["Taxon", "Missing genes"]
                }

    @check_data
    def missing_genes_average(self, ns=None):
        """
        Creates histogram data for average missing taxa
        :param ns: Namespace object. Communicates with main thread
        """

        data = []

        if ns:
            ns.files = len(self.alignments)

        for aln in self.alignments.values():

            if ns:
                # Kill switch
                if ns.stop:
                    raise KillByUser("")
                ns.counter += 1

            data.append(len(set(self.taxa_names) - set(aln.taxa_list)))

        return {"data": data,
                "title": "Distribution of missing taxa",
                "ax_names": ["Number of missing taxa", "Frequency"],
                "table_header": ["Number of missing taxa", "Frequency"]}

    @check_data
    def average_seqsize_per_species(self, ns=None):
        """
        Creates data for the average sequence size for each taxa
        :return: dictionary with arguments for plotting functions
        """

        data_storage = OrderedDict((taxon, []) for taxon in self.taxa_names)

        if ns:
            ns.files = len(self.alignments)

        for aln in self.alignments.values():

            if ns:
                # Kill switch
                if ns.stop:
                    raise KillByUser("")
                ns.counter += 1

            for sp, seq in aln:
                data_storage[sp].append(len(seq.replace("-", "").
                                        replace(aln.sequence_code[1], "")))

        # Adapt y-axis label according to sequence code
        seq_code = aln.sequence_code[0]
        ax_ylabel = "Size (bp)" if seq_code == "DNA" else "Size (residues)"

        data_storage = OrderedDict(sorted(data_storage.items(), reverse=True,
                                   key=lambda t: np.mean(t[1])))

        return {"data": list(data_storage.values()),
                "labels": list(data_storage.keys()),
                "title": "Sequence size distribution per species",
                "ax_names": [None, ax_ylabel]}

    @check_data
    def average_seqsize(self, ns=None):
        """
        Creates data for the average sequence size for the entire data set
        :param ns: Namespace object. Communicates with main thread
        :return:
        """

        data_storage = []

        if ns:
            ns.files = len(self.alignments)

        for aln in self.alignments.values():

            if ns:
                # Kill switch
                if ns.stop:
                    raise KillByUser("")
                ns.counter += 1

            data_storage.append(aln.locus_length)

        # Adapt y-axis label according to sequence code
        seq_code = aln.sequence_code[0]
        ax_xlabel = "Size (bp)" if seq_code == "DNA" else "Size (residues)"

        return {"data": data_storage,
                "title": "Average sequence size distribution",
                "ax_names": [ax_xlabel, "Frequency"],
                "table_header": [ax_xlabel, "Frequency"]}

    @check_data
    def characters_proportion(self, ns=None):
        """
        Creates data for the proportion of nucleotides/residues for the data set
        :param ns: Namespace object. Communicates with main thread
        """

        data_storage = Counter()

        if ns:
            ns.files = len(self.alignments)

        for aln in self.alignments.values():

            if ns:
                #Kill switch
                if ns.stop:
                    raise KillByUser("")
                ns.counter += 1

            for seq in aln.iter_sequences():
                data_storage += Counter(seq.replace("-", "").
                                        replace(self.sequence_code[1], ""))

        # Determine total number of characters
        chars = float(sum(data_storage.values()))

        # Valid characters list
        valid_chars = dna_chars if self.sequence_code[0] == "DNA" else \
            list(aminoacid_table.keys())

        data, xlabels = zip(*[(float(x) / chars, y.upper()) for y, x in
                              data_storage.items() if y in valid_chars])

        title = "Nucleotide proportions" if self.sequence_code[0] == "DNA" \
            else "Amino acid proportions"
        ax_xlabel = "Nucleotide" if self.sequence_code[0] == "DNA" \
            else "Amino acid"

        return {"data": [data],
                "labels": xlabels,
                "title": title,
                "ax_names": [ax_xlabel, "Proportion"],
                "table_header": [ax_xlabel, "Proportion"]}

    @check_data
    def characters_proportion_per_species(self, ns=None):
        """
        Creates data for the proportion of nucleotides/residures per species
        :param ns: Namespace object. Communicates with main thread
        """

        if ns:
            ns.files = len(self.alignments)

        data_storage = OrderedDict((x, Counter()) for x in self.taxa_names)

        for aln in self.alignments.values():

            if ns:
                # Kill switch
                if ns.stop:
                    raise KillByUser("")
                ns.counter += 1

            for sp, seq in aln:
                data_storage[sp] += Counter(seq.replace("-", "").
                                            replace(self.sequence_code[1], ""))

        legend = dna_chars if self.sequence_code[0] == "DNA" else \
            list(aminoacid_table.keys())

        data = [[] for _ in legend]

        for p, char in enumerate(legend):
            for c in data_storage.values():
                chars = float(sum([x for y, x in c.items() if y in legend]))
                data[p].append(float(c[char]) / chars)

        data = np.array(data)

        title = "Nucleotide proportions" if self.sequence_code[0] == "DNA" \
            else "Amino acid proportions"

        ax_ylabel = "Nucleotide" if self.sequence_code[0] == "DNA" \
            else "Amino acid"

        return {"data": data,
                "title": title,
                "labels": list(data_storage.keys()),
                "legend": legend,
                "ax_names": ["Taxa", ax_ylabel],
                "table_header": ["Taxon"] + legend}

    # def _get_reference_data(self, seq_list):
    #     """
    #     Given a reference string, this method will return a list of tuples
    #     for each sequence in seq_list with information on position and
    #     characters of variable sites
    #     :param seq_list: list, containing the strings of the sequences for a
    #     given alignment
    #     :return: a dictionary with sequences as keys and their positions and
    #     characters as tuples
    #     """
    #
    #     # Get reference with least missing data
    #     ref = min(seq_list, key=lambda x: x.count(self.sequence_code[1]) +
    #         x.count(self.gap_symbol))
    #     seq_list.remove(ref)
    #
    #     # pw_data will store the (positions, characters) of the segregating
    #     # sites between the reference and a sequence (e.g: (123, "T"))
    #     self.pw_data = {}
    #     # pw_data_m will store the positions of sites with missing data
    #     self.pw_data_m = {}
    #     self.ref = ref
    #
    #     missing = [self.sequence_code[1], self.gap_symbol]
    #
    #     for seq in seq_list:
    #
    #         # If metrics were already calculated for an identical sequence,
    #         # skip to the next sequence.
    #         if seq in self.pw_data:
    #             continue
    #         else:
    #             self.pw_data[seq] = []
    #             self.pw_data_m[seq] = []
    #
    #         for i in xrange(len(ref)):
    #
    #             # If both chars are missing data or only seq has missing
    #             if ref[i] in missing and seq[i] in missing or \
    #                     ref[i] not in missing and seq[i] in missing:
    #                 self.pw_data_m[seq].append(i)
    #
    #             # If only reference has missing data
    #             elif ref[i] in missing and seq[i] not in missing:
    #                 self.pw_data[seq].append((i, seq[i] + "N"))
    #
    #             # If both chars are not missing data and are different
    #             elif ref[i] != seq[i]:
    #                 self.pw_data[seq].append((i, seq[i]))

        # Convert to sets here, instead of doing it in every pair-wise
        # comparison
        # for k, v in self.pw_data.items():
        #     if k != "ref":
        #         self.pw_data[k] = set(v)
        #
        # for k, v in self.pw_data_m.items():
        #     if k != "ref":
        #         self.pw_data_m[k] = set(v)

    # @pairwise_cache
    # def _get_similarity(self, seq1, seq2, total_len):
    #
    #     # If seq1 is reference sequence
    #     if seq1 == self.ref:
    #
    #         # Get segregating sites
    #         diffs = len([x for x in self.pw_data[seq2] if "N" not in x[1]])
    #
    #         # Get effective len
    #         effective_len = len(seq2) - (len(self.pw_data_m[seq2]))
    #
    #     # If seq2 is reference sequence
    #     elif seq2 == self.ref:
    #
    #         # Get segregating sites
    #         diffs = len([x for x in self.pw_data[seq1] if "N" not in x[1]])
    #
    #         # Get effective len
    #         effective_len = len(seq1) - len(self.pw_data_m[seq1])
    #
    #     else:
    #     # try:
    #         # Get sites with missing data for each sequence. This is done
    #         # before calculating the segregating sites because columns with
    #         # missing data must be removed from the segregating sites set
    #         seq1_m = self.pw_data_m[seq1]
    #         seq2_m = self.pw_data_m[seq2]
    #
    #         # Get union of missing data for both sequences
    #         # m = seq1_m.union(seq2_m)
    #         # missing = len(m)
    #         m_data =[x for x in seq1_m + seq2_m if (x in seq1_m) and
    #                  (x in seq2_m)]
    #         m = len(m_data)
    #
    #         # Get segregating sites for both sequences
    #         seq1_data = self.pw_data[seq1]
    #         seq2_data = self.pw_data[seq2]
    #
    #         # Get segregating sites between the two sequences
    #         #diffs = seq1_data.symmetric_difference(seq2_data)
    #         diffs = len([x[0] for x in seq1_data + seq2_data if
    #                      (x not in seq1_data) or (x not in seq1_data) and
    #                      (x[0] not in m_data)])
    #
    #         effective_len = len(seq1) - m
    #
    #     if effective_len:
    #         return float(effective_len - diffs), float(effective_len)
    #     else:
    #         return None, None

        # except KeyError:
        #     return None, None

    @LookupDatabase
    def _get_similarity(self, seq1, seq2, aln_len):
        """
        Gets the similarity between two sequences in proportion. The
        proportion is calculated here so that sites with only missing
        data/gaps are not accounted in the total length.
        :param seq1: string
        :param seq2: string
        """

        seq1 = np.array(list(seq1))
        seq2 = np.array(list(seq2))

        ef_len_ = (seq1 != self.sequence_code[1]) & \
            (seq1 != self.gap_symbol) & \
            (seq2 != self.sequence_code[1]) & \
            (seq2 != self.gap_symbol)

        sim_ = (seq1 == seq2) & ef_len_

        sim = np.count_nonzero(sim_)
        ef_len = np.count_nonzero(ef_len_)

        return float(sim), float(ef_len)

        # similarity = 0.0
        # effective_len = 0.0
        #
        # missing = [self.sequence_code[1], self.gap_symbol]
        #
        # for c1, c2 in zip(*[seq1, seq2]):
        #     # Ignore comparisons with ONLY missing data / gaps
        #     if c1 in missing or c2 in missing:
        #         continue
        #     elif c1 == c2:
        #         similarity += 1.0
        #         effective_len += 1.0
        #     else:
        #         effective_len += 1.0
        #
        # if effective_len:
        #     return similarity, effective_len
        # else:
        #     return None, None

    # @pairwise_cache
    # def _get_differences(self, seq1, seq2):
    #     """
    #     Returns the proportion of differences between two sequences,
    #     accounting for sites with only missing data / gaps
    #     :param seq1: string, sequence 1
    #     :param seq2: string sequence 2
    #     :return: s: float, proportion of segregating sites
    #     """
    #
    #     s = 0.0
    #     total_len = 0.0
    #
    #     for c1, c2 in zip(*[seq1, seq2]):
    #         # Ignore comparisons with ONLY missing data / gaps
    #         if c1 + c2.replace(self.sequence_code[1], "").replace(
    #                 self.gap_symbol, "") == "":
    #             continue
    #         if c1 != c2:
    #             s += 1.0
    #             total_len += 1.0
    #         else:
    #             total_len += 1.0
    #
    #     return s, total_len

    def _get_informative_sites(self, aln):
        """
        Determines the number of informative sites in an Alignment object
        :param aln: Alignment object
        :return:
        """

        informative_sites = 0

        for column in aln.iter_columns():

            column = Counter([x for x in column if x != aln.sequence_code[1] and
                              x != self.gap_symbol])

            if len(column) > 1:
                # Remove most common and check the length of the remaining
                del column[column.most_common()[0][0]]
                if sum(column.values()) > 1:
                    informative_sites += 1

        return informative_sites

    @check_data
    def sequence_similarity(self, ns=None):
        """
        Creates average sequence similarity data
        :param ns: Namespace object. Communicates with main thread
        """

        self._get_similarity("connect")

        data = []

        if ns:
            ns.files = len(self.alignments)

        for aln in self.alignments.values():

            # self._get_reference_data(list(aln.sequences()))
            if ns:
                ns.counter += 1

            aln_similarities = []

            for seq1, seq2 in itertools.combinations(aln.iter_sequences(), 2):

                if ns:
                    if ns.stop:
                        raise KillByUser("")

                sim, total_len = self._get_similarity(seq1, seq2,
                                                      aln.locus_length)

                if total_len:
                    aln_similarities.append(sim / total_len)

            if aln_similarities:
                data.append(np.mean(aln_similarities) * 100)

        self._get_similarity("disconnect")

        return {"data": data,
                "ax_names": ["Similarity (%)", "Frequency"]}

    @check_data
    def sequence_similarity_per_species(self, ns=None):
        """
        Creates data for a triangular matrix of sequence similarity for pairs
        of taxa

        :param ns: Namespace object. Communicates with main thread
        """

        if ns:
            ns.files = len(self.alignments)

        self._get_similarity("connect")

        # Create matrix for parwise comparisons
        data = [np.empty((len(self.taxa_names), 0)).tolist() for _ in
                range(len(self.taxa_names))]

        taxa_pos = OrderedDict((x, y) for y, x in enumerate(self.taxa_names))

        for aln in self.alignments.values():

            if ns:
                ns.counter += 1

            for tx1, tx2 in itertools.combinations(taxa_pos.keys(), 2):

                if ns:
                    # Kill switch
                    if ns.stop:
                        raise KillByUser("")

                try:
                    seq1, seq2 = aln.get_sequence(tx1), aln.get_sequence(tx2)
                except KeyError:
                    continue

                sim, l = self._get_similarity(seq1, seq2, aln.locus_length)
                if l:
                    data[taxa_pos[tx1]][taxa_pos[tx2]].append(sim / l)

        data = np.array([[np.mean(y) if y else 0. for y in x] for x in data])
        mask = np.tri(data.shape[0], k=0)
        data = np.ma.array(data, mask=mask)

        self._get_similarity("disconnect")

        return {"data": data,
                "color_label": "Pairwise sequence similarity",
                "labels": list(taxa_pos)}

    @check_data
    def sequence_similarity_gene(self, gene_name, window_size, ns=None):

        aln_obj = self.retrieve_alignment(gene_name)

        data = []

        for i in range(0, aln_obj.locus_length, window_size):

            window_similarities = []

            seqs = np.array([[y for y in x[i:i + window_size]] for x in
                             aln_obj.iter_sequences()])

            for seq1, seq2 in itertools.combinations(seqs, 2):

                s, t = self._get_similarity("".join(seq1), "".join(seq2),
                                            window_size)
                if t:
                    sim = s / t
                else:
                    sim = 0

                window_similarities.append(sim * 100)

            if window_similarities:
                data.append(np.mean(window_similarities))

        return {"data": data,
                "title": "Sequence similarity sliding window for gene\n %s"
                         % basename(gene_name),
                "window_size": window_size,
                "ax_names": ["Sequence (bp)", "Similarity (%)"],
                "table_header": ["Sequence (bp)", "Similarity (%)"]}

    @check_data
    def sequence_segregation(self, ns=None, proportions=False):
        """
        Generates data for distribution of segregating sites

        :param proportions: Boolean. If True, use proportions instead of
        absolute values
        :param ns: Namespace object. Communicates with main thread
        """

        if ns:
            ns.files = len(self.alignments)

        data = []

        for aln in self.alignments.values():

            segregating_sites = 0

            if ns:
                # Kill switch
                if ns.stop:
                    raise KillByUser("")
                ns.counter += 1

            for column in aln.iter_columns():

                # Remove gaps and missing characters
                column = set([x for x in column if x != aln.sequence_code[1]
                              and x != self.gap_symbol])

                if len(column) > 1:
                    segregating_sites += 1

            if proportions:
                data.append(
                    float(segregating_sites / float(aln.locus_length)))
                ax_names = ["Segregating sites", "Percentage"]
                real_bin = False
            else:
                data.append(segregating_sites)
                ax_names = ["Segregating sites", "Frequency"]
                real_bin = True

        return {"data": data,
                "ax_names": ax_names,
                "title": "Distribution of segregating sites",
                "table_header": ax_names,
                "real_bin_num": real_bin}

    @check_data
    def sequence_segregation_per_species(self, ns=None):
        """
        Creates a data for a triangular matrix of sequence segregation for
        pairs of taxa

        :param ns: Namespace object. Communicates with the main thread
        """

        if ns:
            ns.files = len(self.alignments)

        self._get_similarity("connect")

        # Create matrix for parwise comparisons
        data = [np.empty((len(self.taxa_names), 0)).tolist() for _ in
                range(len(self.taxa_names))]

        taxa_pos = OrderedDict((x, y) for y, x in enumerate(self.taxa_names))

        for aln in self.alignments.values():

            if ns:
                ns.counter += 1

            for tx1, tx2 in itertools.combinations(taxa_pos.keys(), 2):

                if ns:
                    if ns.stop:
                        raise KillByUser("")

                try:
                    seq1, seq2 = aln.get_sequence(tx1), aln.get_sequence(tx2)
                except KeyError:
                    continue

                s, t = self._get_similarity(seq1, seq2, aln.locus_length)
                if t:
                    aln_diff = t - s
                else:
                    aln_diff = 0

                data[taxa_pos[tx1]][taxa_pos[tx2]].append(aln_diff)

        data = np.array([[np.mean(y) if y else 0. for y in x] for x in data])
        mask = np.tri(data.shape[0], k=0)
        data = np.ma.array(data, mask=mask)

        self._get_similarity("disconnect")

        return {"data": data,
                "labels": list(taxa_pos),
                "color_label": "Segregating sites"}

    @check_data
    def sequence_segregation_gene(self, gene_name, window_size, ns=None):
        """
        Generates data for a sliding window analysis of segregating sites
        :param gene_name: string, name of gene in self.alignments
        :param window_size: size of the sliding window
        """

        aln_obj = self.retrieve_alignment(gene_name)

        data = []

        for i in range(0, aln_obj.locus_length, window_size):

            segregating_sites = 0

            seqs = np.array([[y for y in x[i:i + window_size]] for x in
                             aln_obj.iter_sequences()])

            for column in zip(*seqs):
                column = set([x for x in column if x != aln_obj.sequence_code[1]
                              and x != "-"])

                if len(column) > 1:
                    segregating_sites += 1

            data.append(segregating_sites)

        return {"data": data,
                "title": "Number of segregating sites sliding window for "
                         "gene\n %s" % basename(gene_name),
                "window_size": window_size,
                "ax_names": ["Sequence (bp)", "Segregating sites"],
                "table_header": ["Sequence (bp)", "Segregating sites"]}

    @check_data
    def length_polymorphism_correlation(self, ns=None):
        """
        Generates data for a scatter plot and correlation analysis between
        alignment length and informative sites (polymorphic sites in at least
        two taxa)

        :param ns: Namespace object. Communicates with main thread
        """

        if ns:
            ns.files = len(self.alignments)

        data_length = []
        data_inf = []

        for aln in self.alignments.values():

            if ns:
                # Kill switch
                if ns.stop:
                    raise KillByUser("")
                ns.counter += 1

            # Get informative sites for alignment
            inf_sites = self._get_informative_sites(aln)
            data_inf.append(inf_sites)

            # Get size
            data_length.append(aln.locus_length)

        return {"data": [data_length, data_inf],
                "title": "Correlation between alignment length and number of "
                         "variable sites",
                "ax_names": ["Alignment length", "Informative sites"],
                "table_header": ["Alignment length", "Informative sites"],
                "correlation": True}

    @check_data
    def allele_frequency_spectrum(self, ns=None, proportions=False):
        """
        Generates data for the allele frequency spectrum of the entire
        alignment data set. Here, multiple alignments are effectively treated
        as a single one. This method is exclusive of DNA sequence type and
        supports IUPAC ambiguity codes
        """

        # Make check for sequence type consistency here for TriStats.py. In
        # TriFusion the check is made before calling this method
        if self.sequence_code[0] != "DNA":
            return {"exception": InvalidSequenceType}

        data = []

        if ns:
            ns.files = len(self.alignments)

        for aln in self.alignments.values():

            if ns:
                if ns.stop:
                    raise KillByUser("")
                ns.counter += 1

            for column in aln.iter_columns():

                col = [iupac_conv[x] for x in column if
                                  x != aln.sequence_code[1] and
                                  x != self.gap_symbol]
                col_len = len(col)

                # Remove gaps and missing characters
                column = Counter(col)

                # Consider only bi-allelic SNPs
                if len(column) == 2:
                    # Remove most common and check the length of the remaining
                    del column[column.most_common()[0][0]]
                    # Append number of derived alleles
                    if proportions:
                        data.append(float(sum(column.values())) /
                                    float(col_len))
                    else:
                        data.append(sum(column.values()))

        return {"data": data,
                "title": "Allele frequency spectrum",
                "ax_names": ["Derived allele frequency", "Frequency"],
                "table_header": ["Derived allele frequency", "Frequency"],
                "real_bin_num": True}

    @check_data
    def allele_frequency_spectrum_gene(self, gene_name):
        """
        Generates data for the allele frequency spectrum of the gene
        specified by gene_name
        :param gene_name: string, gene name present in the AlignmentList object
        """

        # Make check for sequence type consistency here for TriStats.py. In
        # TriFusion the check is made before calling this method
        if self.sequence_code[0] != "DNA":
            return {"exception": InvalidSequenceType}

        aln = self.retrieve_alignment(gene_name)
        data = []

        for column in aln.iter_columns():

            # Remove gaps and missing characters
            column = Counter([iupac_conv[x] for x in column if
                              x != aln.sequence_code[1] and
                              x != self.gap_symbol])

            # Consider only bi-allelic SNPs
            if len(column) == 2:
                # Remove most common and check the length of the remaining
                del column[column.most_common()[0][0]]
                # Append number of derived alleles
                data.append(sum(column.values()))

        return {"data": data,
                "title": "Allele frequency spectrum",
                "ax_names": ["Derived allele frequency", "Frequency"],
                "table_header": ["Derived allele frequency", "Frequency"],
                "real_bin_num": True}

    @check_data
    def taxa_distribution(self, ns=None):
        """
        Generates data for a distribution of taxa frequency across alignments
        """

        if ns:
            ns.files = len(self.alignments)

        data = []

        for aln in self.alignments.values():

            if ns:
                if ns.stop:
                    raise KillByUser("")
                ns.counter += 1

            # Get number of taxa
            data.append(len(aln.taxa_list))

        return {"data": data,
                "title": "Distribution of taxa frequency",
                "ax_names": ["Number of taxa", "Frequency"],
                "table_header": ["Number of taxa", "Frequency"],
                "real_bin_num": True}

    @check_data
    def cumulative_missing_genes(self, ns=None):
        """
        Generates data for a distribution of the maximum number of genes
        available for consecutive thresholds of missing data.
        :param ns: Namespace object. Communicates with main thread
        """

        size_storage = []
        data = []

        if ns:
            ns.files = len(self.alignments)

        # total number of taxa in data set
        taxa = float(len(self.taxa_names))

        for aln in self.alignments.values():

            if ns:
                # Kill switch
                if ns.stop:
                    raise KillByUser
                ns.counter += 1

            # Get number of taxa
            size_storage.append((float(len(aln.taxa_list)) / taxa) * 100)

        labels = []
        for i in xrange(0, 105, 5):

            # Get percentage
            data.append(len([x for x in size_storage if x > i]))
            labels.append(str(i))

        return {"data": [data],
                "title": "Gene frequency for decreasing values of missing "
                         "data",
                "labels": labels,
                "ax_names": ["Minimum taxa representation", "Gene frequency"],
                "table_header": ["Percentage", "Frequency"]}

    @staticmethod
    def _mad_based_outlier(p, threshold=3.5):
        """
        An outlier detection method based on median absolute deviation. This
        code was adapted from http://stackoverflow.com/a/22357811/1990165.
        The usage of media is much less biased that the mean and is robust to
        smaller data sets.
        :param p: Num of observations
        :param threshold: modified Z-score to use as a threshold.
        """

        if len(p.shape) == 1:
            p = p[:, None]
        # Get sample median.
        median = np.median(p, axis=0)
        diff = np.sum((p - median) ** 2, axis=-1)
        diff = np.sqrt(diff)
        med_abs_deviation = np.median(diff)

        # The 0.6745is a constant that makes values roughly equivalent in
        # units to standard deviations.
        z_score = 0.6745 * diff / med_abs_deviation

        return z_score > threshold

    @check_data
    def outlier_missing_data(self, ns=None):
        """
        Get data for outlier detection of genes based on the distribution of
        average missing data per gene. Data points will be based on the
        proportion of missing data symbols out of the possible total. For
        example, in an alignment with three taxa, each with 100 sites,
        the total possible missing data is 300 (100 * 3). Here, missing data
        will be gathered from all taxa and a proportion will be calculated
        based n the total possible

        :param ns: Namespace object. Communicates with main thread
        """

        if ns:
            ns.files = len(self.alignments)

        data_labels = []
        data_points = []

        for gn, aln in self.alignments.items():

            if ns:
                if ns.stop:
                    raise KillByUser("")
                ns.counter += 1

            total_len = aln.locus_length * len(aln.taxa_list)
            gn_data = 0

            for seq in aln.iter_sequences():
                gn_data += seq.count(self.sequence_code[1]) + \
                          seq.count(self.gap_symbol)

            m_data = float(gn_data) / float(total_len)
            data_points.append(m_data)
            data_labels.append(gn)

        data_points = np.asarray(data_points)
        data_labels = np.asarray(data_labels)

        # Get outliers
        outliers_points = data_points[self._mad_based_outlier(data_points)]
        # Get outlier labels
        outliers_labels = list(data_labels[self._mad_based_outlier(
            data_points)])

        return {"data": data_points,
                "title": "Missing data outlier gene detection",
                "outliers": outliers_points,
                "outliers_labels": outliers_labels,
                "ax_names": ["Proportion of missing data", "Frequency"]}

    @check_data
    def outlier_missing_data_sp(self, ns=None):
        """
        Gets data for outlier detection of species based on missing data. For
        this analysis, genes for which a taxa is completely absent will be
        ignored for the calculations of that taxa. The reason for this,
        is that including genes where the taxon is absent would bias the
        outlier detection towards taxa that have low prevalence in the data
        set, even if they have low missing data in the alignments where they
        are present.
        :param ns: Namespace object. Communicates with main thread
        """

        if ns:
            ns.files = len(self.alignments)

        data = dict((tx, []) for tx in self.taxa_names)

        for aln in self.alignments.values():

            if ns:
                if ns.stop:
                    raise KillByUser("")
                ns.counter += 1

            total_len = aln.locus_length

            # Get missing data for every taxon
            for tx in data:
                if tx in aln.taxa_list:
                    seq = aln.get_sequence(tx)
                    m_data = float(seq.count(self.sequence_code[1]) +
                                   seq.count(self.gap_symbol)) / \
                        float(total_len)
                    data[tx].append(m_data)

        # Get average for each taxon
        for tx, vals in data.items():
            data[tx] = np.mean(vals)

        # Prepara data for plotting
        data_points = []
        data_labels = []
        for tx, vals in data.items():
            data_points.append(vals)
            data_labels.append(tx)

        data_points = np.asarray(data_points)
        data_labels = np.asarray(data_labels)

        # Get outliers
        outliers_points = data_points[self._mad_based_outlier(data_points)]
        # Get outlier taxa
        outlier_labels = list(data_labels[self._mad_based_outlier(data_points)])

        return {"data": data_points,
                "title": "Missing data outlier taxa detection",
                "outliers": outliers_points,
                "outliers_labels": outlier_labels,
                "ax_names": ["Proportion of missing data", "Frequency"]}

    @check_data
    def outlier_segregating(self, ns=None):
        """
        Generates data for the outlier detection of genes based on
        segregating sites. The data will be based on the number of alignments
        columns with a variable number of sites, excluding gaps and missing
        data

        :param ns: Namespace object. Communicates with main thread
        """

        if ns:
            ns.files = len(self.alignments)

        data_points = []
        data_labels = []

        for aln in self.alignments.values():

            if ns:
                if ns.stop:
                    raise KillByUser("")
                ns.counter += 1

            segregating_sites = 0

            for column in aln.iter_columns():

                # Remove gaps and missing characters
                column = set([x for x in column if x != aln.sequence_code[1] and
                              x != self.gap_symbol])

                if len(column) > 1:
                    segregating_sites += 1

            # Get proportion of segregating sites for current alignment
            data_points.append(float(segregating_sites) /
                               float(aln.locus_length))
            data_labels.append(aln.name)

        data_points = np.asarray(data_points)
        data_labels = np.asarray(data_labels)

        # Get outliers
        outliers_points = data_points[self._mad_based_outlier(data_points)]
        # Get outlier taxa
        outlier_labels = list(data_labels[self._mad_based_outlier(data_points)])

        return {"data": data_points,
                "title": "Sequence variation outlier gene detection",
                "outliers": outliers_points,
                "outliers_labels": outlier_labels,
                "ax_names": ["Proportion of segregating sites", "Frequency"]}

    @check_data
    def outlier_segregating_sp(self, ns=None):
        """
        Generates data for the outlier detection of species based on their
        average pair-wise proportion of segregating sites. Comparisons
        with gaps or missing data are ignored
        :param ns: Namespace object. Communicates with main thread
        """

        if ns:
            ns.files = len(self.alignments)

        self._get_similarity("connect")

        data = OrderedDict((tx, []) for tx in self.taxa_names)

        for aln in self.alignments.values():

            if ns:
                ns.counter += 1

            for tx1, tx2 in itertools.combinations(data.keys(), 2):

                if ns:
                    if ns.stop:
                        raise KillByUser("")

                try:
                    seq1, seq2 = aln.get_sequence(tx1), aln.get_sequence(tx2)
                except KeyError:
                    continue

                s, t_len = self._get_similarity(seq1, seq2, aln.locus_length)

                if t_len:
                    s_data = (t_len - s) / t_len
                else:
                    s_data = 0

                data[tx1].append(s_data)
                data[tx2].append(s_data)

        for tx, vals in data.items():
            data[tx] = np.mean(vals)

        # Prepara data for plotting
        data_points = []
        data_labels = []
        for tx, vals in data.items():
            data_points.append(vals)
            data_labels.append(tx)

        data_points = np.asarray(data_points)
        data_labels = np.asarray(data_labels)

        # Get outliers
        outliers_points = data_points[self._mad_based_outlier(data_points)]
        # Get outlier taxa
        outlier_labels = list(data_labels[self._mad_based_outlier(data_points)])

        self._get_similarity("disconnect")

        return {"data": data_points,
                "title": "Sequence variation outlier taxa detection",
                "outliers": outliers_points,
                "outliers_labels": outlier_labels,
                "ax_names": ["Proportion of segregating sites", "Frequency"]}

    @check_data
    def outlier_sequence_size(self, ns=None):
        """
        Generates data for the outlier detection of genes based on their
        sequence length (excluding missing data)
        :param ns: Namespace object. Communicates with main thread
        """

        if ns:
            ns.files = len(self.alignments)

        data_labels = []
        data_points = []

        for gn, aln in self.alignments.items():

            if ns:
                if ns.stop:
                    raise KillByUser("")
                ns.counter += 1

            gn_l = []

            for seq in aln.iter_sequences():
                gn_l.append(len(seq.
                              replace(aln.sequence_code[1], "").
                              replace(self.gap_symbol, "")))

            gn_avg = np.mean(gn_l)
            data_points.append(gn_avg)
            data_labels.append(gn)

        data_points = np.asarray(data_points)
        data_labels = np.asarray(data_labels)

        # Get outliers
        outliers_points = data_points[self._mad_based_outlier(data_points)]
        # Get outliers labels
        outliers_labels = list(data_labels[self._mad_based_outlier(
            data_points)])

        return {"data": data_points,
                "title": "Sequence size outlier gene detection",
                "outliers": outliers_points,
                "outliers_labels": outliers_labels,
                "ax_names": ["Sequence size", "Frequency"]}

    @check_data
    def outlier_sequence_size_sp(self, ns=None):
        """
        Generates data for the outlier detection of species based on their
        sequence length (excluding missing data)
        :param ns: Namespace object. Communicates with main thread
        """

        if ns:
            ns.files = len(self.alignments)

        data = dict((tx, []) for tx in self.taxa_names)

        for aln in self.alignments.values():

            if ns:
                if ns.stop:
                    raise KillByUser("")
                ns.counter += 1

            for tx in data:
                if tx in aln.taxa_list:
                    seq = aln.get_sequence(tx)
                    s_data = len(seq.replace(aln.sequence_code[1], "").
                                 replace(self.gap_symbol, ""))
                    data[tx].append(s_data)

        # Get average for each taxon
        for tx, vals in data.items():
            data[tx] = np.mean(vals)

        # Preparing data for plotting
        data_points = []
        data_labels = []
        for tx, vals in data.items():
            data_points.append(vals)
            data_labels.append(tx)

        data_points = np.asarray(data_points)
        data_labels = np.asarray(data_labels)

        # Get outliers
        outliers_points = data_points[self._mad_based_outlier(data_points)]
        # Get outlier taxa
        outlier_labels = list(data_labels[self._mad_based_outlier(data_points)])

        return {"data": data_points,
                "title": "Sequence size outlier taxa detection",
                "outliers": outliers_points,
                "outliers_labels": outlier_labels,
                "ax_names": ["Sequence size", "Frequency"]}

__author__ = "Diogo N. Silva"
__credits__ = ["Diogo N. Silva", "Tiago F. Jesus"]