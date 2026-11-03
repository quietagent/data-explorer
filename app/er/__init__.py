"""ER tooling shared with the two CLI scripts in tools/.

tokenizer, parser, model, inference, layout and the two renderers are copied
from tools/sql_to_drawio.py and tools/sql_to_erd.py. Each module's docstring
lists the few places where the app's copy differs.
"""

from .inference import finalize, guess_one, names_table, pk_like
from .model import Column, Model, Rel, Table
from .parser import Parser
from .tokenizer import tokenize

__all__ = ['Column', 'Model', 'Parser', 'Rel', 'Table', 'finalize', 'guess_one', 'names_table',
           'parse_sql', 'pk_like', 'tokenize']


def parse_sql(texts):
    """Parse one SQL string (or several) into a finalized Model."""
    model = Model()
    for sql in ([texts] if isinstance(texts, str) else texts):
        Parser(model, tokenize(sql)).run()
    finalize(model)
    return model
