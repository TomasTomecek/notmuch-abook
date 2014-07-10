#!/usr/bin/env python

## Filename: notmuch_addresses.py
## Copyright (C) 2010-11 Jesse Rosenthal
## Author: Jesse Rosenthal <jrosenthal@jhu.edu>

## This file is free software; you can redistribute it and/or modify
## it under the terms of the GNU General Public License as published
## by the Free Software Foundation; either version 2, or (at your
## option) any later version.

## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.

## NOTE: This script requires the notmuch python bindings.
"""
Notmuch Addressbook utility

Usage:
  notmuch_abook.py -h
  notmuch_abook.py [-v] [-c CONFIG] create
  notmuch_abook.py [-v] [-c CONFIG] update
  notmuch_abook.py [-v] [-c CONFIG] lookup [ -f FORMAT ] <match>
  notmuch_abook.py [-v] [-c CONFIG] changename <address> <name>
  notmuch_abook.py [-v] [-c CONFIG] export [ -f FORMAT ] [ -s SORT ] [<filename>]
  notmuch_abook.py [-v] [-c CONFIG] import [ -f FORMAT ] [ -r ] [<filename>]

Options:
  -h --help                   Show this help message and exit
  -v --verbose                Show full stacktraces on error
  -c CONFIG, --config CONFIG  Path to notmuch configuration file
  -f FORMAT, --format FORMAT  Format for name/address (see below) [default: email]
  -s SORT, --sort SORT        Whether to sort by name or address [default: name]
  -r, --replace               If present, then replace the current contents with
                              the imported contents.  If not then merge - add new
                              addresses, and update the name associated with
                              existing addresses.

Commands:

  create               Create a new database.
  update               Update the database with a new email (on stdin).
  lookup <match>       Lookup an address in the database.  The match can be
                       an email address or part of a name.
  changename <address> <name>
                       Change the name associated with an email address.
  export [<filename>]  Export database, to filename if given or to stdout if not.
  import [<filename>]  Import into database, from filename if given or from stdin
                       if not.

Valid values for the FORMAT are:

* abook - Give output in abook compatible format so it can be easily parsed
          by other programs.  The format is EMAIL<Tab>NAME
* csv   - Give output as CSV (comma separated values). NAME,EMAIL
* email - Give output in a format that can be used when composing an email.
          So NAME <EMAIL>

The database to use is set in the notmuch config file.
"""

import os.path
import sys
import docopt
from io import open
import notmuch
import re
import sqlite3
import ConfigParser
import email.parser
import email.utils
# use unicode csv if available
try:
    import unicodecsv as csv
except ImportError:
    import csv

VALID_FORMATS = ['abook', 'csv', 'email']


class InvalidOptionError(Exception):
    """An option wasn't valid."""


class NotMuchConfig(object):
    def __init__(self, config_file):
        if config_file is None:
            config_file = os.environ.get('NOTMUCH_CONFIG', '~/.notmuch-config')

        # set a default for ignorefile
        self.config = ConfigParser.ConfigParser({'ignorefile': None})
        self.config.read(os.path.expanduser(config_file))

    def get(self, section, key):
        return self.config.get(section, key)


class Ignorer(object):
    def __init__(self, config):
        self.ignorefile = config.get('addressbook', 'ignorefile')
        self.ignore_regexes = None
        self.ignore_substrings = None

    def create_regexes(self):
        if self.ignorefile is None:
            return
        self.ignore_regexes = []
        self.ignore_substrings = []
        for line in open(self.ignorefile):
            line = line.strip()
            if not line or line.startswith('#'):
                continue  # skip blank lines and comments
            if line.startswith('/') and line.endswith('/'):
                self.ignore_regexes.append(re.compile(line.strip('/'), re.IGNORECASE))
            else:
                self.ignore_substrings.append(line)

    def ignore_address(self, address):
        """Check if this email address should be ignored.

        Return True if it should be ignored, or False otherwise."""
        if self.ignorefile is None:
            return False
        if self.ignore_regexes is None:
            self.create_regexes()
        substring_match = any(substr in address for substr in self.ignore_substrings)
        if substring_match:
            return True
        return any(regex.search(address) for regex in self.ignore_regexes)


class MailParser(object):
    def __init__(self):
        self.addresses = dict()

    def parse_mail(self, m):
        """
        function used to extract headers from a email.message or
        notmuch.message email object yields address tuples
        """
        addrs = []
        if isinstance(m, email.message.Message):
            get_header = m.get
        else:
            get_header = m.get_header
        for h in ('to', 'from', 'cc', 'bcc'):
            v = get_header(h)
            if v:
                addrs.append(v)
        for addr in email.utils.getaddresses(addrs):
            name = addr[0].strip('; ')
            address = addr[1].lower().strip(';\'" ')
            if (address and address not in self.addresses):
                self.addresses[address] = name
                yield (name, address)


class NotmuchAddressGetter(object):
    """Get all addresses from notmuch, based on information information from
    the user's $HOME/.notmuch-config file.
    """

    def __init__(self, config):
        """
        """
        self.db_path = config.get("database", "path")
        self._mp = MailParser()

    def _get_all_messages(self):
        notmuch_db = notmuch.Database(self.db_path)
        query = notmuch.Query(notmuch_db, "NOT tag:junk AND NOT folder:drafts AND NOT tag:deleted")
        return query.search_messages()

    def generate(self):
        msgs = self._get_all_messages()
        for m in msgs:
            for addr in self._mp.parse_mail(m):
                yield addr


class SQLiteStorage():
    """SQL Storage backend"""
    def __init__(self, config):
        self.__path = config.get("addressbook", "path")

    def connect(self):
        """
        creates a new connection to the database and returns a cursor
        throws an error if the database does not exists
        """
        if not os.path.exists(self.__path):
            raise IOError("Database '%s' does not exists" % (self.__path,))
        return sqlite3.connect(self.__path, isolation_level="DEFERRED")

    def create(self):
        """
        create a new database
        """
        if os.path.exists(self.__path):
            raise IOError("Can't create database at '%s'. File exists." %
                          (self.__path,))
        else:
            with sqlite3.connect(self.__path) as c:
                cur = c.cursor()
                cur.execute("CREATE VIRTUAL TABLE AddressBook USING fts4(Name, Address)")
                cur.execute("CREATE VIEW AddressBookView AS SELECT * FROM addressbook")
                cur.executescript(
                    "CREATE TRIGGER insert_into_ab " +
                    "INSTEAD OF INSERT ON AddressBookView " +
                    "BEGIN" +
                    " SELECT RAISE(ABORT, 'column name is not unique')" +
                    "   FROM addressbook" +
                    "  WHERE address = new.address;" +
                    " INSERT INTO addressbook VALUES(new.name, new.address);" +
                    "END;")

    def init(self, gen):
        """
        populates the database with all addresses from address book
        """
        n = 0
        with self.connect() as cur:
            cur.execute("PRAGMA synchronous = OFF")
            for elt in gen():
                try:
                    cur.execute("INSERT INTO AddressBookView VALUES(?,?)", elt)
                    n += 1
                except sqlite3.IntegrityError:
                    pass
            cur.commit()
        return n

    def update(self, addr, ignorer, replace=False):
        """
        updates the database with a new mail address tuple

        replace: if the email address already exists then replace the name with the new name
        """
        if ignorer.ignore_address(addr[1]):
            return False
        try:
            with self.connect() as c:
                cur = c.cursor()
                if replace:
                    present = cur.execute("SELECT 1 FROM AddressBook WHERE address = ?", [addr[1]])
                    if present:
                        cur.execute(
                            "UPDATE AddressBook SET name = ? WHERE address = ?", addr)
                    else:
                        cur.execute("INSERT INTO AddressBookView VALUES(?,?)", addr)
                else:
                    cur.execute("INSERT INTO AddressBookView VALUES(?,?)", addr)
                return True
        except sqlite3.IntegrityError:
            return False

    def lookup(self, match):
        """
        lookup an address from the given match in database
        """
        with self.connect() as c:
            # so we can access results via dictionary
            c.row_factory = sqlite3.Row
            cur = c.cursor()
            for res in cur.execute(
                """SELECT * FROM AddressBook WHERE AddressBook MATCH '"%s*"'"""
                    % match).fetchall():
                yield res

    def fetchall(self, order_by):
        """
        Fetch all entries from the database.
        """
        with self.connect() as c:
            c.row_factory = sqlite3.Row
            cur = c.cursor()
            for res in cur.execute("SELECT * FROM AddressBook ORDER BY %s" % order_by).fetchall():
                yield res

    def change_name(self, address, name):
        """
        Change the name associated with an email address
        """
        with self.connect() as c:
            cur = c.cursor()
            cur.execute(
                "UPDATE AddressBook SET name = '%s' WHERE address = '%s'" %
                (name, address))
            return True

    def delete_db(self):
        """
        Delete the database
        """
        if os.path.exists(self.__path):
            os.remove(self.__path)


def format_address(address, output_format):
    if output_format == 'abook':
        return "%s\t%s" % (address['Address'], address['Name'])
    elif output_format == 'email':
        return email.utils.formataddr((address['Name'], address['Address']))
    else:
        raise InvalidOptionError('Unknown format: %s' % output_format)


def decode_line(line, input_format):
    if input_format == 'abook':
        if '\t' in line:
            address, name = line.split('\t')
        else:
            address, name = line, ''
    elif input_format == 'email':
        name, address = email.utils.parseaddr(line)
    else:
        raise InvalidOptionError('Unknown format: %s' % input_format)
    return name, address


def print_address_list(address_list, output_format, out=None):
    if out is None:
        out = sys.stdout
    if output_format == 'csv':
        try:
            writer = csv.writer(out)
            for address in address_list:
                writer.writerow((address['Name'], address['Address']))
        except UnicodeEncodeError as e:
            print >> sys.stderr, "Caught UnicodeEncodeError: %s" % e
            print >> sys.stderr, "Installing unicodecsv will probably fix this"
            return
    else:
        for address in address_list:
            out.write(format_address(address, output_format) + '\n')


def import_address_list(db, ignorer, replace_all, input_format, infile=None):
    if infile is None:
        infile = sys.stdin
    if replace_all:
        db.delete_db()
        db.create()
    if input_format == 'csv':
        try:
            reader = csv.reader(infile)
            for row in reader:
                db.update(row, ignorer, replace=(not replace_all))
        except UnicodeEncodeError as e:
            print >> sys.stderr, "Caught UnicodeEncodeError: %s" % e
            print >> sys.stderr, "Installing unicodecsv will probably fix this"
            return
    else:
        for line in infile:
            name_addr = decode_line(line.strip(), input_format)
            db.update(name_addr, ignorer, replace=(not replace_all))


def create_act(db, cf):
    db.create()
    nm_mailgetter = NotmuchAddressGetter(cf)
    n = db.init(nm_mailgetter.generate)
    print "added %d addresses" % n


def update_act(db, ignorer, verbose):
    n = 0
    m = email.message_from_file(sys.stdin)
    for addr in MailParser().parse_mail(m):
        if db.update(addr, ignorer):
            n += 1
    if verbose:
        print "added %d addresses" % n


def lookup_act(match, output_format, db):
    print_address_list(db.lookup(match), output_format)


def export_action(output_format, sort, db, filename=None):
    out = None
    try:
        if filename:
            out = open(filename, mode='w', encoding='utf-8')
        print_address_list(db.fetchall(sort), output_format, out)
    finally:
        if filename and out:
            out.close()


def import_action(input_format, replace, db, ignorer, filename=None):
    infile = None
    try:
        if filename:
            infile = open(filename, mode='r', encoding='utf-8')
        import_address_list(db, ignorer, replace, input_format, infile)
    finally:
        if filename and infile:
            infile.close()


def run():
    options = docopt.docopt(__doc__)

    if options['--format'] not in VALID_FORMATS:
        print >> sys.stderr, '%s is not a valid output option.' % options['--format']
        return 2

    try:
        cf = NotMuchConfig(options['--config'])
        if cf.get("addressbook", "backend") == "sqlite3":
            db = SQLiteStorage(cf)
        else:
            print "Database backend '%s' is not implemented." % \
                cf.get("addressbook", "backend")
        ignorer = Ignorer(cf)

        if options['create']:
            create_act(db, cf)
        elif options['update']:
            update_act(db, ignorer, options['--verbose'])
        elif options['lookup']:
            lookup_act(options['<match>'], options['--format'], db)
        elif options['changename']:
            db.change_name(options['<address>'], options['<name>'])
        elif options['export']:
            export_action(options['--format'], options['--sort'], db, options['<filename>'])
        elif options['import']:
            import_action(options['--format'], options['--replace'], db, ignorer, options['<filename>'])
    except Exception as exc:
        if options['--verbose']:
            import traceback
            traceback.print_exc()
        else:
            print exc
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(run())
