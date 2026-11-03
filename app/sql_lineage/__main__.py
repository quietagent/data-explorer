"""Entry point for `python -m sql_lineage ...`. It only hands over to cli.main()."""

import sys

if sys.version_info < (3, 11):
    sys.exit("SQL Lineage needs Python 3.11 or newer.")

from sql_lineage.cli import main

sys.exit(main())
