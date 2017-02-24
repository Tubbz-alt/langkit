from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import sys

import libfoolang


ctx = libfoolang.AnalysisContext()

text = '1 + 2'
u = ctx.get_from_buffer('main.txt', text)
if u.diagnostics:
    for d in u.diagnostics:
        print(d)
    sys.exit(1)

u.populate_lexical_env()
print('Evaluating {}'.format(text))
print('result = {}'.format(u.root.p_result))
