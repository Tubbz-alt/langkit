"""
Module to gather the logic to lower Lkt syntax trees to Langkit internal data
structures.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
import os.path
from typing import (
    Any, ClassVar, Dict, List, Optional, Set, Tuple, Type, TypeVar, Union,
    cast
)

import liblktlang as L

from langkit.compile_context import CompileCtx
from langkit.compiled_types import (
    ASTNodeType, AbstractNodeData, BaseField, CompiledType, CompiledTypeRepo,
    EnumNodeAlternative, Field, T, TypeRepo, UserField
)
from langkit.diagnostics import DiagnosticError, check_source_language, error
from langkit.expressions import AbstractProperty, Property, PropertyDef
from langkit.lexer import (Action, Alt, Case, Ignore, Lexer, LexerToken,
                           Literal, Matcher, NoCaseLit, Pattern, RuleAssoc,
                           TokenFamily, WithSymbol, WithText, WithTrivia)
import langkit.names as names
from langkit.parsers import (Discard, DontSkip, Grammar, List as PList, Null,
                             Opt, Or, Parser, Pick, Predicate, Skip, _Row,
                             _Token, _Transform)


CompiledTypeOrDefer = Union[CompiledType, TypeRepo.Defer]


def get_trait(decl: L.TypeDecl, trait_name: str) -> Optional[L.TypeDecl]:
    """
    Return the trait named ``trait_name`` on declaration ``decl``.
    """
    for trait in decl.f_traits:
        trait_decl: L.TypeDecl = trait.p_designated_type
        if trait_decl.f_syn_name.text == trait_name:
            return trait_decl
    return None


def pattern_as_str(str_lit: L.StringLit) -> str:
    """
    Return the regexp string associated to this string literal node.
    """
    return json.loads(str_lit.text[1:])


def parse_static_bool(ctx: CompileCtx, expr: L.Expr) -> bool:
    """
    Return the bool value that this expression denotes.
    """
    with ctx.lkt_context(expr):
        check_source_language(isinstance(expr, L.RefId)
                              and expr.text in ('false', 'true'),
                              'Boolean literal expected')

    return expr.text == 'true'


def load_lkt(lkt_file: str) -> L.AnalysisUnit:
    """
    Load a Lktlang source file and return the closure of Lkt units referenced.
    Raise a DiagnosticError if there are parsing errors.

    :param lkt_file: Name of the file to parse.
    """
    units_map = OrderedDict()
    diagnostics = []

    def process_unit(unit: L.AnalysisUnit) -> None:
        if unit.filename in units_map:
            return

        # Register this unit and its diagnostics
        units_map[unit.filename] = unit
        for d in unit.diagnostics:
            diagnostics.append((unit, d))

        # Recursively process the units it imports. In case of parsing error,
        # just stop the recursion: the collection of diagnostics is enough.
        if not unit.diagnostics:
            import_stmts = list(unit.root.f_imports)
            for imp in import_stmts:
                process_unit(imp.p_referenced_unit)

    # Load ``lkt_file`` and all the units it references, transitively
    process_unit(L.AnalysisContext().get_from_file(lkt_file))

    # If there are diagnostics, forward them to the user. TODO: hand them to
    # langkit.diagnostic.
    if diagnostics:
        for u, d in diagnostics:
            print('{}:{}'.format(os.path.basename(u.filename), d))
        raise DiagnosticError()
    return list(units_map.values())


def find_toplevel_decl(ctx: CompileCtx,
                       lkt_units: List[L.AnalysisUnit],
                       node_type: type,
                       label: str) -> L.FullDecl:
    """
    Look for a top-level declaration of type ``node_type`` in the given units.

    If none or several are found, emit error diagnostics. Return the associated
    full declaration.

    :param lkt_units: List of units where to look.
    :param node_type: Node type to look for.
    :param label: Human readable string for what to look for. Used to create
        diagnostic mesages.
    """
    result = None
    for unit in lkt_units:
        for decl in unit.root.f_decls:
            if not isinstance(decl.f_decl, node_type):
                continue

            with ctx.lkt_context(decl):
                if result is not None:
                    check_source_language(
                        False,
                        'only one {} allowed (previous found at {}:{})'.format(
                            label,
                            os.path.basename(result.unit.filename),
                            result.sloc_range.start
                        )
                    )
                result = decl

    with ctx.lkt_context(lkt_units[0].root):
        check_source_language(result is not None, 'missing {}'.format(label))

    return result


class AnnotationSpec:
    """
    Synthetic description of how a declaration annotation works.
    """

    def __init__(self, name: str, unique: bool, require_args: bool,
                 default_value: Any = None):
        """
        :param name: Name of the annotation (``foo`` for the ``@foo``
            annotation).
        :param unique: Whether this annotation can appear at most once for a
            given declaration.
        :param require_args: Whether this annotation requires arguments.
        :param default_value: For unique annotations, value to use in case the
            annotation is absent.
        """
        self.name = name
        self.unique = unique
        self.require_args = require_args
        self.default_value = default_value if unique else []

    def interpret(self, ctx: CompileCtx,
                  args: List[L.Expr],
                  kwargs: Dict[str, L.Expr]) -> Any:
        """
        Subclasses must override this in order to interpret an annotation.

        This method must validate and interpret ``args`` and ``kwargs``, and
        return a value suitable for annotations processing.

        :param args: Positional arguments for the annotation.
        :param kwargs: Keyword arguments for the annotation.
        """
        raise NotImplementedError

    def parse_single_annotation(self,
                                ctx: CompileCtx,
                                result: Dict[str, Any],
                                annotation: L.DeclAnnotation) -> None:
        """
        Parse an annotation node according to this spec. Add the result to
        ``result``.
        """
        check_source_language(
            self.name not in result or not self.unique,
            'This annotation cannot appear multiple times'
        )

        # Check that parameters presence comply to the spec
        if not annotation.f_params:
            check_source_language(not self.require_args,
                                  'Arguments required for this annotation')
            value = self.interpret(ctx, [], {})
        else:
            check_source_language(self.require_args,
                                  'This annotation accepts no argument')

            # Collect positional and named arguments
            args = []
            kwargs = {}
            for param in annotation.f_params.f_params:
                with ctx.lkt_context(param):
                    if param.f_name:
                        name = param.f_name.text
                        check_source_language(name not in kwargs,
                                              'Named argument repeated')
                        kwargs[name] = param.f_value

                    else:
                        check_source_language(not kwargs,
                                              'Positional arguments must'
                                              ' appear before named ones')
                        args.append(param.f_value)

            # Evaluate this annotation
            value = self.interpret(ctx, args, kwargs)

        # Store annotation evaluation into the result
        if self.unique:
            result[self.name] = value
        else:
            result.setdefault(self.name, [])
            result[self.name].append(value)


class FlagAnnotationSpec(AnnotationSpec):
    """
    Convenience subclass for flags.
    """
    def __init__(self, name: str):
        super().__init__(
            name, unique=True, require_args=False, default_value=False
        )

    def interpret(self,
                  ctx: CompileCtx,
                  args: List[L.Expr],
                  kwargs: Dict[str, L.Expr]) -> Any:
        return True


class SpacingAnnotationSpec(AnnotationSpec):
    """
    Interpreter for @spacing annotations for lexer.
    """
    def __init__(self) -> None:
        super().__init__('spacing', unique=False, require_args=True)

    def interpret(self,
                  ctx: CompileCtx,
                  args: List[L.Expr],
                  kwargs: Dict[str, L.Expr]) -> Tuple[str, str]:

        check_source_language(len(args) == 2 and not kwargs,
                              'Exactly two positional arguments expected')
        for f in args:
            # Check that we only have RefId nodes, but do not attempt to
            # translate them to TokenFamily instances: at the point we
            # interpret annotations, the set of token families is not ready
            # yet.
            check_source_language(isinstance(f, L.RefId),
                                  'Token family name expected')
        left, right = args
        return (left, right)


class TokenAnnotationSpec(AnnotationSpec):
    """
    Interpreter for @text/symbol/trivia annotations for tokens.
    """
    def __init__(self, name: str):
        super().__init__(name, unique=True, require_args=True)

    def interpret(self,
                  ctx: CompileCtx,
                  args: List[L.Expr],
                  kwargs: Dict[str, L.Expr]) -> Tuple[bool, bool]:
        check_source_language(not args, 'No positional argument allowed')

        try:
            start_ignore_layout = kwargs.pop('start_ignore_layout')
        except KeyError:
            start_ignore_layout = False
        else:
            start_ignore_layout = parse_static_bool(ctx, start_ignore_layout)

        try:
            end_ignore_layout = kwargs.pop('end_ignore_layout')
        except KeyError:
            end_ignore_layout = False
        else:
            end_ignore_layout = parse_static_bool(ctx, end_ignore_layout)

        check_source_language(
            not kwargs,
            'Invalid arguments: {}'.format(', '.join(sorted(kwargs)))
        )

        return (start_ignore_layout, end_ignore_layout)


# Annotation specs for grammar rules

token_cls_map = {'text': WithText,
                 'trivia': WithTrivia,
                 'symbol': WithSymbol}


@dataclass
class ParsedAnnotations:
    """
    Namespace object to hold annotation parsed values.
    """

    annotations: ClassVar[List[AnnotationSpec]]


@dataclass
class GrammarRuleAnnotations(ParsedAnnotations):
    main_rule: bool
    annotations = [FlagAnnotationSpec('main_rule')]


@dataclass
class TokenAnnotations(ParsedAnnotations):
    text: Tuple[bool, bool]
    trivia: Tuple[bool, bool]
    symbol: Tuple[bool, bool]
    newline_after: bool
    pre_rule: bool
    ignore: bool
    annotations = [TokenAnnotationSpec('text'),
                   TokenAnnotationSpec('trivia'),
                   TokenAnnotationSpec('symbol'),
                   FlagAnnotationSpec('newline_after'),
                   FlagAnnotationSpec('pre_rule'),
                   FlagAnnotationSpec('ignore')]


@dataclass
class LexerAnnotations(ParsedAnnotations):
    spacing: Tuple[L.RefId, L.RefId]
    track_indent: bool
    annotations = [SpacingAnnotationSpec(),
                   FlagAnnotationSpec('track_indent')]


@dataclass
class BaseNodeAnnotations(ParsedAnnotations):
    has_abstract_list: bool
    annotations = [FlagAnnotationSpec('has_abstract_list')]


@dataclass
class NodeAnnotations(BaseNodeAnnotations):
    abstract: bool
    annotations = BaseNodeAnnotations.annotations + [
        FlagAnnotationSpec('abstract')
    ]


@dataclass
class EnumNodeAnnotations(BaseNodeAnnotations):
    qualifier: bool
    annotations = BaseNodeAnnotations.annotations + [
        FlagAnnotationSpec('qualifier')
    ]


@dataclass
class FieldAnnotations(ParsedAnnotations):
    abstract: bool
    null_field: bool
    parse_field: bool
    annotations = [FlagAnnotationSpec('abstract'),
                   FlagAnnotationSpec('null_field'),
                   FlagAnnotationSpec('parse_field')]


def check_no_annotations(full_decl: L.FullDecl) -> None:
    """
    Check that the declaration has no annotation.
    """
    check_source_language(
        len(full_decl.f_decl_annotations) == 0, 'No annotation allowed'
    )


AnyPA = TypeVar('AnyPA', bound=ParsedAnnotations)


def parse_annotations(ctx: CompileCtx,
                      annotation_class: Type[AnyPA],
                      full_decl: L.FullDecl) -> AnyPA:
    """
    Parse annotations according to the specs in
    ``annotation_class.annotations``. Return a ParsedAnnotations that contains
    the interpreted annotation values for each present annotation.

    :param annotation_class: ParsedAnnotations subclass for the result, holding
        the annotation specs to guide parsing.
    :param full_decl: Declaration whose annotations are to be parsed.
    """
    # Build a mapping for all specs
    specs_map: Dict[str, AnnotationSpec] = {}
    for s in annotation_class.annotations:
        assert s.name not in specs_map
        specs_map[s.name] = s

    # Process annotations
    values: Dict[str, Any] = {}
    for a in full_decl.f_decl_annotations:
        name = a.f_name.text
        spec = specs_map.get(name)
        with ctx.lkt_context(a):
            if spec is None:
                check_source_language(
                    False, 'Invalid annotation: {}'.format(name)
                )
            else:
                spec.parse_single_annotation(ctx, values, a)

    # Use the default value for absent annotations
    for s in annotation_class.annotations:
        values.setdefault(s.name, s.default_value)

    # Create the namespace object to hold results
    return annotation_class(**values)  # type: ignore


def create_lexer(ctx: CompileCtx, lkt_units: List[L.AnalysisUnit]) -> Lexer:
    """
    Create and populate a lexer from a Lktlang unit.

    :param lkt_units: Non-empty list of analysis units where to look for the
        grammar.
    """
    # Look for the LexerDecl node in top-level lists
    full_lexer = find_toplevel_decl(ctx, lkt_units, L.LexerDecl, 'lexer')
    with ctx.lkt_context(full_lexer):
        lexer_annot = parse_annotations(ctx, LexerAnnotations, full_lexer)

    patterns: Dict[names.Name, str] = {}
    """
    Mapping from pattern names to the corresponding regular expression.
    """

    token_family_sets: Dict[names.Name, Set[Action]] = {}
    """
    Mapping from token family names to the corresponding sets of tokens that
    belong to this family.
    """

    token_families: Dict[names.Name, TokenFamily] = {}
    """
    Mapping from token family names to the corresponding token families.  We
    build this late, once we know all tokens and all families.
    """

    tokens: Dict[names.Name, Action] = {}
    """
    Mapping from token names to the corresponding tokens.
    """

    rules: List[Union[RuleAssoc, Tuple[Matcher, Action]]] = []
    pre_rules: List[Tuple[Matcher, Action]] = []
    """
    Lists of regular and pre lexing rules for this lexer.
    """

    newline_after: List[Action] = []
    """
    List of tokens after which we must introduce a newline during unparsing.
    """

    def ignore_constructor(start_ignore_layout: bool,
                           end_ignore_layout: bool) -> Action:
        """
        Adapter to build a Ignore instance with the same API as WithText
        constructors.
        """
        del start_ignore_layout, end_ignore_layout
        return Ignore()

    def process_family(f: L.LexerFamilyDecl) -> None:
        """
        Process a LexerFamilyDecl node. Register the token family and process
        the rules it contains.
        """
        with ctx.lkt_context(f):
            # Create the token family, if needed
            name = names.Name.from_lower(f.f_syn_name.text)
            token_set = token_family_sets.setdefault(name, set())

            for r in f.f_rules:
                check_source_language(
                    isinstance(r.f_decl, L.GrammarRuleDecl),
                    'Only lexer rules allowed in family blocks'
                )
                process_token_rule(r, token_set)

    def process_token_rule(
        r: L.FullDecl,
        token_set: Optional[Set[Action]] = None
    ) -> None:
        """
        Process the full declaration of a GrammarRuleDecl node: create the
        token it declares and lower the optional associated lexing rule.

        :param r: Full declaration for the GrammarRuleDecl to process.
        :param token_set: If this declaration appears in the context of a token
            family, this adds the new token to this set.  Must be left to None
            otherwise.
        """
        with ctx.lkt_context(r):
            rule_annot: TokenAnnotations = parse_annotations(
                ctx, TokenAnnotations, r
            )

            # Gather token action info from the annotations. If absent,
            # fallback to WithText.
            token_cons = None
            start_ignore_layout = False
            end_ignore_layout = False
            if rule_annot.ignore:
                token_cons = ignore_constructor
            for name in ('text', 'trivia', 'symbol'):
                annot = getattr(rule_annot, name)
                if not annot:
                    continue
                start_ignore_layout, end_ignore_layout = annot

                check_source_language(token_cons is None,
                                      'At most one token action allowed')
                token_cons = token_cls_map[name]
            is_pre = rule_annot.pre_rule
            if token_cons is None:
                token_cons = WithText

            # Create the token and register it where needed: the global token
            # mapping, its token family (if any) and the "newline_after" group
            # if the corresponding annotation is present.
            token_lower_name = r.f_decl.f_syn_name.text
            token_name = names.Name.from_lower(token_lower_name)

            check_source_language(
                token_lower_name not in ('termination', 'lexing_failure'),
                '{} is a reserved token name'.format(token_lower_name)
            )
            check_source_language(token_name not in tokens,
                                  'Duplicate token name')

            token = token_cons(start_ignore_layout, end_ignore_layout)
            tokens[token_name] = token
            if token_set is not None:
                token_set.add(token)
            if rule_annot.newline_after:
                newline_after.append(token)

            # Lower the lexing rule, if present
            matcher_expr = r.f_decl.f_expr
            if matcher_expr is not None:
                rule = (lower_matcher(matcher_expr), token)
                if is_pre:
                    pre_rules.append(rule)
                else:
                    rules.append(rule)

    def process_pattern(full_decl: L.FullDecl) -> None:
        """
        Process a pattern declaration.

        :param full_decl: Full declaration for the ValDecl to process.
        """
        check_no_annotations(full_decl)
        decl = full_decl.f_decl
        lower_name = decl.f_syn_name.text
        name = names.Name.from_lower(lower_name)

        with ctx.lkt_context(decl):
            check_source_language(name not in patterns,
                                  'Duplicate pattern name')
            check_source_language(decl.f_decl_type is None,
                                  'Patterns must have automatic types in'
                                  ' lexers')
            check_source_language(
                isinstance(decl.f_val, L.StringLit)
                and decl.f_val.p_is_regexp_literal,
                'Pattern string literal expected'
            )
            # TODO: use StringLit.p_denoted_value when properly implemented
            patterns[name] = pattern_as_str(decl.f_val)

    def lower_matcher(expr: L.GrammarExpr) -> Matcher:
        """
        Lower a token matcher to our internals.
        """
        with ctx.lkt_context(expr):
            if isinstance(expr, L.TokenLit):
                return Literal(json.loads(expr.text))
            elif isinstance(expr, L.TokenNoCaseLit):
                return NoCaseLit(json.loads(expr.text))
            elif isinstance(expr, L.TokenPatternLit):
                return Pattern(pattern_as_str(expr))
            else:
                error('Invalid lexing expression')

    def lower_token_ref(ref: L.RefId) -> Action:
        """
        Return the Token that `ref` refers to.
        """
        with ctx.lkt_context(ref):
            token_name = names.Name.from_lower(ref.text)
            check_source_language(token_name in tokens,
                                  'Unknown token: {}'.format(token_name.lower))
            return tokens[token_name]

    def lower_family_ref(ref: L.RefId) -> TokenFamily:
        """
        Return the TokenFamily that `ref` refers to.
        """
        with ctx.lkt_context(ref):
            name_lower = ref.text
            name = names.Name.from_lower(name_lower)
            check_source_language(
                name in token_families,
                'Unknown token family: {}'.format(name_lower)
            )
            return token_families[name]

    def lower_case_alt(alt: L.BaseLexerCaseRuleAlt) -> Alt:
        """
        Lower the alternative of a case lexing rule.
        """
        prev_token_cond = None
        if isinstance(alt, L.LexerCaseRuleCondAlt):
            prev_token_cond = [lower_token_ref(ref)
                               for ref in alt.f_cond_exprs]
        return Alt(prev_token_cond=prev_token_cond,
                   send=lower_token_ref(alt.f_send.f_sent),
                   match_size=int(alt.f_send.f_match_size.text))

    # Go through all rules to register tokens, their token families and lexing
    # rules.
    for full_decl in full_lexer.f_decl.f_rules:
        with ctx.lkt_context(full_decl):
            if isinstance(full_decl, L.LexerFamilyDecl):
                # This is a family block: go through all declarations inside it
                process_family(full_decl)

            elif isinstance(full_decl, L.FullDecl):
                # There can be various types of declarations in lexers...
                decl = full_decl.f_decl

                if isinstance(decl, L.GrammarRuleDecl):
                    # Here, we have a token declaration, potentially associated
                    # with a lexing rule.
                    process_token_rule(full_decl)

                elif isinstance(decl, L.ValDecl):
                    # This is the declaration of a pattern
                    process_pattern(full_decl)

                else:
                    check_source_language(False,
                                          'Unexpected declaration in lexer')

            elif isinstance(full_decl, L.LexerCaseRule):
                syn_alts = list(full_decl.f_alts)

                # This is a rule for conditional lexing: lower its matcher and
                # its alternative rules.
                matcher = lower_matcher(full_decl.f_expr)
                check_source_language(
                    len(syn_alts) == 2 and
                    isinstance(syn_alts[0], L.LexerCaseRuleCondAlt) and
                    isinstance(syn_alts[1], L.LexerCaseRuleDefaultAlt),
                    'Invalid case rule topology'
                )
                rules.append(Case(matcher,
                                  lower_case_alt(syn_alts[0]),
                                  lower_case_alt(syn_alts[1])))

            else:
                # The grammar should make the following dead code
                assert False, 'Invalid lexer rule: {}'.format(full_decl)

    # Create the LexerToken subclass to define all tokens and token families
    items: Dict[str, Union[Action, TokenFamily]] = {}
    for name, token in tokens.items():
        items[name.camel] = token
    for name, token_set in token_family_sets.items():
        tf = TokenFamily(*list(token_set))
        token_families[name] = tf
        items[name.camel] = tf
    token_class = type('Token', (LexerToken, ), items)

    # Create the Lexer instance and register all patterns and lexing rules
    result = Lexer(token_class,
                   lexer_annot.track_indent,
                   pre_rules)
    for name, regexp in patterns.items():
        result.add_patterns((name.lower, regexp))
    result.add_rules(*rules)

    # Register spacing/newline rules
    for tf1, tf2 in lexer_annot.spacing:
        result.add_spacing((lower_family_ref(tf1),
                            lower_family_ref(tf2)))
    result.add_newline_after(*newline_after)

    return result


def create_grammar(ctx: CompileCtx,
                   lkt_units: List[L.AnalysisUnit]) -> Grammar:
    """
    Create a grammar from a set of Lktlang units.

    Note that this only initializes a grammar and fetches relevant declarations
    in the Lktlang unit. The actual lowering on grammar rules happens in a
    separate pass: see lower_all_lkt_rules.

    :param lkt_units: Non-empty list of analysis units where to look for the
        grammar.
    """
    # Look for the GrammarDecl node in top-level lists
    full_grammar = find_toplevel_decl(ctx, lkt_units, L.GrammarDecl, 'grammar')

    # No annotation allowed for grammars
    with ctx.lkt_context(full_grammar):
        check_no_annotations(full_grammar)

    # Get the list of grammar rules. This is where we check that we only have
    # grammar rules, that their names are unique, and that they have valid
    # annotations.
    all_rules = OrderedDict()
    main_rule_name = None
    for full_rule in full_grammar.f_decl.f_rules:
        with ctx.lkt_context(full_rule):
            r = full_rule.f_decl

            # Make sure we have a grammar rule
            check_source_language(isinstance(r, L.GrammarRuleDecl),
                                  'grammar rule expected')
            rule_name = r.f_syn_name.text

            # Register the main rule if the appropriate annotation is present
            a = parse_annotations(ctx, GrammarRuleAnnotations, full_rule)
            if a.main_rule:
                check_source_language(main_rule_name is None,
                                      'only one main rule allowed')
                main_rule_name = rule_name

            all_rules[rule_name] = r.f_expr

    # Now create the result grammar. We need exactly one main rule for that.
    with ctx.lkt_context(full_grammar):
        check_source_language(main_rule_name is not None,
                              'Missing main rule (@main_rule annotation)')
    result = Grammar(main_rule_name, ctx.lkt_loc(full_grammar))

    # Translate rules (all_rules) later, as node types are not available yet
    result._all_lkt_rules.update(all_rules)
    return result


def lower_grammar_rules(ctx: CompileCtx) -> None:
    """
    Translate syntactic L rules to Parser objects.
    """
    grammar = ctx.grammar

    # Build a mapping for all tokens registered in the lexer. Use lower case
    # names, as this is what the concrete syntax is supposed to use.
    tokens = {token.name.lower: token
              for token in ctx.lexer.tokens.tokens}

    # Build a mapping for all nodes created in the DSL. We cannot use T (the
    # TypeRepo instance) as types are not processed yet.
    nodes = {n.raw_name.camel: n
             for n in CompiledTypeRepo.astnode_types}

    # For every non-qualifier enum node, build a mapping from value names
    # (camel cased) to the corresponding enum node subclass.
    enum_nodes = {
        node: node._alternatives_map
        for node in nodes.values()
        if node.is_enum_node and not node.is_bool_node
    }

    def denoted_string_literal(string_lit: L.StringLit) -> str:
        return eval(string_lit.text)

    def resolve_node_ref_or_none(
        node_ref: Optional[L.RefId]
    ) -> Optional[ASTNodeType]:
        """
        Convenience wrapper around resolve_node_ref to handle None values.
        """
        if node_ref is None:
            return None
        return resolve_node_ref(node_ref)

    def resolve_node_ref(node_ref: L.RefId) -> ASTNodeType:
        """
        Helper to resolve a node name to the actual AST node.

        :param node_ref: Node that is the reference to the AST node.
        """
        if isinstance(node_ref, L.DotExpr):
            # Get the altenatives mapping for the prefix_node enum node
            prefix_node = resolve_node_ref(node_ref.f_prefix)
            with ctx.lkt_context(node_ref.f_prefix):
                try:
                    alt_map = enum_nodes[prefix_node]
                except KeyError:
                    error('Non-qualifier enum node expected (got {})'
                          .format(prefix_node.dsl_name))

            # Then resolve the alternative
            suffix = node_ref.f_suffix.text
            with ctx.lkt_context(node_ref.f_suffix):
                try:
                    return alt_map[suffix]
                except KeyError:
                    error('Unknown enum node alternative: {}'.format(suffix))

        elif isinstance(node_ref, L.GenericTypeRef):
            check_source_language(
                node_ref.f_type_name.text == u'ASTList',
                'Bad generic type name: only ASTList is valid in this context'
            )

            params = node_ref.f_params
            check_source_language(
                len(params) == 1,
                '1 type argument expected, got {}'.format(len(params))
            )
            return resolve_node_ref(params[0]).list

        elif isinstance(node_ref, L.SimpleTypeRef):
            return resolve_node_ref(node_ref.f_type_name)

        else:
            assert isinstance(node_ref, L.RefId)
            with ctx.lkt_context(node_ref):
                node_name = node_ref.text
                try:
                    return nodes[node_name]
                except KeyError:
                    error('Unknown node: {}'.format(node_name))

    def lower(rule: L.GrammarExpr) -> Optional[Parser]:
        """
        Helper to lower one parser.

        :param rule: Grammar rule to lower.
        """
        # For convenience, accept null input rules, as we generally want to
        # forward them as-is to the lower level parsing machinery.
        if rule is None:
            return None

        loc = ctx.lkt_loc(rule)
        with ctx.lkt_context(rule):
            if isinstance(rule, L.ParseNodeExpr):
                node = resolve_node_ref(rule.f_node_name)

                # Lower the subparsers
                subparsers = [lower(subparser)
                              for subparser in rule.f_sub_exprs]

                # Qualifier nodes are a special case: we produce one subclass
                # or the other depending on whether the subparsers accept the
                # input.
                if node.is_bool_node:
                    return Opt(*subparsers, location=loc).as_bool(node)

                # Likewise for enum nodes
                elif node.base and node.base.is_enum_node:
                    return _Transform(_Row(*subparsers, location=loc),
                                      node,
                                      location=loc)

                # For other nodes, always create the node when the subparsers
                # accept the input.
                else:
                    return _Transform(parser=_Row(*subparsers), typ=node,
                                      location=loc)

            elif isinstance(rule, L.GrammarToken):
                token_name = rule.f_token_name.text
                try:
                    val = tokens[token_name]
                except KeyError:
                    check_source_language(
                        False, 'Unknown token: {}'.format(token_name)
                    )

                match_text = ''
                if rule.f_expr:
                    # The grammar is supposed to mainain this invariant
                    assert isinstance(rule.f_expr, L.TokenLit)
                    match_text = denoted_string_literal(rule.f_expr)

                return _Token(val=val, match_text=match_text, location=loc)

            elif isinstance(rule, L.TokenLit):
                return _Token(denoted_string_literal(rule), location=loc)

            elif isinstance(rule, L.GrammarList):
                return PList(
                    lower(rule.f_expr),
                    empty_valid=rule.f_kind.text == '*',
                    list_cls=resolve_node_ref_or_none(rule.f_list_type),
                    sep=lower(rule.f_sep),
                    location=loc
                )

            elif isinstance(rule, (L.GrammarImplicitPick,
                                   L.GrammarPick)):
                return Pick(*[lower(subparser) for subparser in rule.f_exprs],
                            location=loc)

            elif isinstance(rule, L.GrammarRuleRef):
                return getattr(grammar, rule.f_node_name.text)

            elif isinstance(rule, L.GrammarOrExpr):
                return Or(*[lower(subparser)
                            for subparser in rule.f_sub_exprs],
                          location=loc)

            elif isinstance(rule, L.GrammarOpt):
                return Opt(lower(rule.f_expr), location=loc)

            elif isinstance(rule, L.GrammarOptGroup):
                return Opt(*[lower(subparser) for subparser in rule.f_expr],
                           location=loc)

            elif isinstance(rule, L.GrammarExprList):
                return Pick(*[lower(subparser) for subparser in rule],
                            location=loc)

            elif isinstance(rule, L.GrammarDiscard):
                return Discard(lower(rule.f_expr), location=loc)

            elif isinstance(rule, L.GrammarNull):
                return Null(resolve_node_ref(rule.f_name), location=loc)

            elif isinstance(rule, L.GrammarSkip):
                return Skip(resolve_node_ref(rule.f_name), location=loc)

            elif isinstance(rule, L.GrammarDontSkip):
                return DontSkip(lower(rule.f_expr),
                                lower(rule.f_dont_skip),
                                location=loc)

            elif isinstance(rule, L.GrammarPredicate):
                check_source_language(
                    isinstance(rule.f_prop_ref, L.DotExpr),
                    'Invalid property reference'
                )
                node = resolve_node_ref(rule.f_prop_ref.f_prefix)
                prop_name = rule.f_prop_ref.f_suffix.text
                try:
                    prop = node.get_abstract_node_data_dict()[prop_name]
                except KeyError:
                    check_source_language(
                        False,
                        '{} has no {} property'
                        .format(node.dsl_name, prop_name)
                    )
                return Predicate(lower(rule.f_expr), prop, location=loc)

            else:
                raise NotImplementedError('unhandled parser: {}'.format(rule))

    for name, rule in grammar._all_lkt_rules.items():
        grammar._add_rule(name, lower(rule))


class LktTypesLoader:
    """
    Helper class to instanciate ``CompiledType`` for all types described in
    Lkt.
    """

    syntax_types: Dict[names.Name, L.FullDecl]
    compiled_types: Dict[names.Name, Optional[CompiledTypeOrDefer]]

    def __init__(self, ctx: CompileCtx, lkt_units: List[L.AnalysisUnit]):
        """
        :param ctx: Context in which to create these types.
        :param lkt_units: Non-empty list of analysis units where to look for
            type declarations.
        """
        self.ctx = ctx

        # Go through all units, build a map for all type definitions, indexed
        # by Name. This first pass allows the check of unique names.
        self.syntax_types = {}
        for unit in lkt_units:
            for full_decl in unit.root.f_decls:
                if not isinstance(full_decl.f_decl, L.TypeDecl):
                    continue
                name_str = full_decl.f_decl.f_syn_name.text
                name = names.Name.from_camel(name_str)
                check_source_language(
                    name not in self.syntax_types,
                    'Duplicate type name: {}'.format(name_str)
                )
                self.syntax_types[name] = full_decl

        # Map indexed by type Name. Unvisited types are absent, fully processed
        # types have an entry with the corresponding CompiledType, and
        # currently processed types have an entry associated with None.
        self.compiled_types = {}

        # Pre-fill it with builtin types. Use camel-case type repo names,
        # except for the character type, which gets a special name in Lkt.
        for type_name, t in CompiledTypeRepo.type_dict.items():
            dsl_name = (names.Name('Char')
                        if t.is_character_type
                        else names.Name.from_camel(type_name))
            self.compiled_types[dsl_name] = t

        # String is a special shortcut
        self.compiled_types[names.Name('String')] = T.Character.array

        # Now create CompiledType instances for each user type. To properly
        # handle node derivation, recurse on bases first and reject inheritance
        # loops.
        for name in sorted(self.syntax_types):
            self.create_type_from_name(name, defer=False)

    def resolve_type_ref(self,
                         ref: L.TypeRef,
                         defer: bool) -> CompiledTypeOrDefer:
        """
        Fetch the CompiledType instance corresponding to the given type
        reference.

        :param ref: Type reference to resolve.
        :param defer: If True and this type is not lowered yet, return a
            TypeRepo.Defer instance. Lower the type if necessary in all other
            cases.
        """
        with self.ctx.lkt_context(ref):
            if isinstance(ref, L.SimpleTypeRef):
                return self.create_type_from_name(
                    names.Name.from_camel(ref.f_type_name.text),
                    defer
                )

            elif isinstance(ref, L.GenericTypeRef):
                check_source_language(
                    isinstance(ref.f_type_name, L.RefId),
                    'Invalid generic type'
                )
                gen_type = ref.f_type_name.text
                gen_args = list(ref.f_params)
                if gen_type == 'ASTList':
                    check_source_language(
                        len(gen_args) == 1,
                        'Exactly one type argument expected'
                    )
                    elt_type = self.resolve_type_ref(gen_args[0], defer)
                    return cast(ASTNodeType, elt_type).list

                else:
                    error('Unknown generic type')

            else:
                raise NotImplementedError(
                    'Unhandled type reference: {}'.format(ref)
                )

    def create_type_from_name(self,
                              name: names.Name,
                              defer: bool) -> CompiledTypeOrDefer:
        """
        Fetch the CompiledType instance corresponding to the given type
        reference.

        :param name: Name of the type to create.
        :param defer: If True and this type is not lowered yet, return a
            TypeRepo.Defer instance. Lower the type if necessary in all other
            cases.
        """
        # First, look for an already translated type (this can be a builtin
        # type).
        result = self.compiled_types.get(name)
        if result is not None:
            return result

        # Then, look for user-defined types, which may not have been translated
        # yet.
        full_decl = self.syntax_types.get(name)
        if full_decl is None:
            error('Invalid type name: {}'.format(name.camel))

        # If it's present, we know it wasn't translated yet, so return a defer
        # stub for it when requested. Otherwise, we will translate it now.
        if defer:
            return getattr(T, name.camel)
        decl = full_decl.f_decl

        # Directly return already created CompiledType instances and raise an
        # error for cycles in the type inheritance graph.
        compiled_type = self.compiled_types.get(name, "not-visited")

        if isinstance(compiled_type, CompiledType):
            return compiled_type

        with self.ctx.lkt_context(decl):
            check_source_language(
                compiled_type is not None,
                'Type inheritance loop detected'
            )
            self.compiled_types[name] = None

            # Dispatch now to the appropriate type creation helper
            if isinstance(decl, L.BasicClassDecl):
                specs = (EnumNodeAnnotations
                         if isinstance(decl, L.EnumClassDecl)
                         else NodeAnnotations)
                result = self.create_node(
                    name, decl,
                    parse_annotations(self.ctx, specs, full_decl)
                )

            else:
                raise NotImplementedError(
                    'Unhandled type declaration: {}'.format(decl)
                )

            self.compiled_types[name] = result
            return result

    def lower_base_field(
        self,
        full_decl: L.FullDecl,
        allowed_field_types: Tuple[Type[AbstractNodeData], ...]
    ) -> BaseField:
        """
        Lower the BaseField described in ``decl``.

        :param allowed_field_types: Set of types allowed for the fields to
            load.
        """
        decl = full_decl.f_decl
        annotations = parse_annotations(self.ctx, FieldAnnotations, full_decl)
        field_type = self.resolve_type_ref(decl.f_decl_type, defer=True)

        cls: Type[BaseField]
        if decl.f_default_val:
            raise NotImplementedError(
                'Field default values not handled yet'
            )

        if annotations.parse_field:
            cls = Field
            kwargs = {'abstract': annotations.abstract,
                      'null': annotations.null_field}
        else:
            check_source_language(
                not annotations.abstract,
                'Regular fields cannot be abstract'
            )
            check_source_language(
                not annotations.null_field,
                'Regular fields cannot be null'
            )
            cls = UserField
            kwargs = {}

        check_source_language(
            issubclass(cls, allowed_field_types),
            'Invalid field type in this context'
        )

        return cls(type=field_type, doc=self.ctx.lkt_doc(full_decl), **kwargs)

    def lower_fields(self,
                     decls: L.DeclBlock,
                     allowed_field_types: Tuple[Type[AbstractNodeData], ...]) \
            -> List[Tuple[names.Name, AbstractNodeData]]:
        """
        Lower the fields described in the given DeclBlock node.

        :param decls: Declarations to process.
        :param allowed_field_types: Set of types allowed for the fields to
        load.
        """
        result = []
        for full_decl in decls:
            decl = full_decl.f_decl

            # Check field name conformity
            name_text = decl.f_syn_name.text
            check_source_language(
                not name_text.startswith('_'),
                'Underscore-prefixed field names are not allowed'
            )
            check_source_language(
                name_text.lower() == name_text,
                'Field names must be lower-case'
            )
            name = names.Name.from_lower(name_text)

            field = self.lower_base_field(full_decl, allowed_field_types)
            field.location = self.ctx.lkt_loc(decl)
            result.append((name, cast(AbstractNodeData, field)))
        return result

    def create_node(self,
                    name: names.Name,
                    decl: L.ClassDecl,
                    annotations: BaseNodeAnnotations) -> ASTNodeType:
        """
        Create an ASTNodeType instance.

        :param name: DSL name for this node type.
        :param decl: Corresponding declaration node.
        :param annotations: Annotations for this declaration.
        """
        is_enum_node = isinstance(annotations, EnumNodeAnnotations)
        loc = self.ctx.lkt_loc(decl)

        # Resolve the base node (if any)
        check_source_language(
            not is_enum_node or decl.f_base_type is not None,
            'Enum nodes need a base node'
        )
        base = (cast(ASTNodeType,
                     self.resolve_type_ref(decl.f_base_type, defer=False))
                if decl.f_base_type else None)
        check_source_language(
            base is None or not base.is_enum_node,
            'Inheritting from an enum node is forbiddn'
        )

        # Make sure the Node trait is used exactly once, on the root node
        node_trait = get_trait(decl, "Node")
        if base is None:
            check_source_language(
                node_trait is not None,
                'The root node should derive from Node'
            )
            if CompiledTypeRepo.root_grammar_class is not None:
                check_source_language(
                    False,
                    'There can be only one root node ({})'.format(
                        CompiledTypeRepo.root_grammar_class.dsl_name
                    )
                )

        # This is a token node if either the TokenNode trait is implemented or
        # if the base node is a token node itself.
        is_token_node = get_trait(decl, "TokenNode") is not None

        # Lower fields. Regular nodes can hold all types of fields, but token
        # nodes and enum nodes can hold only user field and properties.
        allowed_field_types = (
            (UserField, PropertyDef)
            if is_token_node or is_enum_node
            else (AbstractNodeData, )
        )
        fields = self.lower_fields(decl.f_decls, allowed_field_types)

        # For qualifier enum nodes, add the synthetic "as_bool" abstract
        # property that each alternative will override.
        is_bool_node = False
        if (
            isinstance(annotations, EnumNodeAnnotations)
            and annotations.qualifier
        ):
            prop = AbstractProperty(
                type=T.Bool, public=True,
                doc='Return whether this node is present'
            )
            prop.location = loc
            fields.append((names.Name('As_Bool'), prop))
            is_bool_node = True

        result = ASTNodeType(
            name,
            location=loc,
            doc=self.ctx.lkt_doc(decl.parent),
            base=base,
            fields=fields,
            is_abstract=(not isinstance(annotations, NodeAnnotations)
                         or annotations.abstract),
            is_token_node=is_token_node,
            has_abstract_list=annotations.has_abstract_list,
            is_enum_node=is_enum_node,
            is_bool_node=is_bool_node,
        )

        # Create alternatives for enum nodes
        if isinstance(annotations, EnumNodeAnnotations):
            self.create_enum_node_alternatives(
                alternatives=sum(
                    (list(b.f_decls) for b in decl.f_branches), []
                ),
                enum_node=result,
                qualifier=annotations.qualifier
            )

        return result

    def create_enum_node_alternatives(
        self,
        alternatives: List[L.EnumClassAltDecl],
        enum_node: ASTNodeType,
        qualifier: bool
    ) -> None:
        """
        Create ASTNodeType instances for the alternatives of an enum node.

        :param alternatives: Declarations for the alternatives to lower.
        :param enum_node: Enum node that owns these alternatives.
        :param qualifier: Whether this enum node has the "@qualifier"
            annotation.
        """
        # RA22-015: initialize this to True for enum nodes directly in
        # ASTNodeType's constructor.
        enum_node.is_type_resolved = True

        enum_node._alternatives = []
        enum_node._alternatives_map = {}

        # All enum classes must have at least one alternative, except those
        # with the "@qualifier" annotation, which implies automatic
        # alternatives.
        if qualifier:
            check_source_language(
                not len(alternatives),
                'Enum nodes with @qualifier cannot have explicit alternatives'
            )
            alternatives = [
                EnumNodeAlternative(names.Name(alt_name),
                                    enum_node,
                                    None,
                                    enum_node.location)
                for alt_name in ('Present', 'Absent')
            ]
        else:
            check_source_language(
                len(alternatives),
                'Missing alternatives for this enum node'
            )
            alternatives = [
                EnumNodeAlternative(names.Name.from_camel(alt.f_syn_name.text),
                                    enum_node,
                                    None,
                                    self.ctx.lkt_loc(alt))
                for alt in alternatives
            ]

        # Now create the ASTNodeType instances themselves
        for i, alt in enumerate(alternatives):
            # Override the abstract "as_bool" property that all qualifier enum
            # nodes define.
            fields = []
            if qualifier:
                is_present = i == 0
                prop = Property(is_present)
                prop.location = enum_node.location
                fields.append(('as_bool', prop))

            alt.alt_node = ASTNodeType(
                name=alt.full_name, location=enum_node.location, doc='',
                base=enum_node,
                fields=fields,
                dsl_name='{}.{}'.format(enum_node.dsl_name,
                                        alt.base_name.camel)
            )

        # Finally create enum node-local indexes to easily fetch the
        # ASTNodeType instances later on.
        enum_node._alternatives = [alt.alt_node for alt in alternatives]
        enum_node._alternatives_map = {alt.base_name.camel: alt.alt_node
                                       for alt in alternatives}


def create_types(ctx: CompileCtx, lkt_units: List[L.AnalysisUnit]) -> None:
    """
    Create types from Lktlang units.

    :param ctx: Context in which to create these types.
    :param lkt_units: Non-empty list of analysis units where to look for type
        declarations.
    """
    LktTypesLoader(ctx, lkt_units)
