import operator
import random
import os
import copy
import abc
import contrib.pyparsing as pp

from .. import utils
from . import generators, exceptions

TRUNCATE = 1024

Sep = pp.Optional(pp.Literal(":")).suppress()


v_integer = pp.Word(pp.nums)\
    .setName("integer")\
    .setParseAction(lambda toks: int(toks[0]))


v_literal = pp.MatchFirst(
    [
        pp.QuotedString(
            "\"",
            escChar="\\",
            unquoteResults=True,
            multiline=True
        ),
        pp.QuotedString(
            "'",
            escChar="\\",
            unquoteResults=True,
            multiline=True
        ),
    ]
)

v_naked_literal = pp.MatchFirst(
    [
        v_literal,
        pp.Word("".join(i for i in pp.printables if i not in ",:\n@\'\""))
    ]
)


class _Token(object):
    """
        A specification token. Tokens are immutable.
    """
    __metaclass__ = abc.ABCMeta

    @classmethod
    def expr(klass): # pragma: no cover
        """
            A parse expression.
        """
        return None

    @abc.abstractmethod
    def spec(self): # pragma: no cover
        """
            A parseable specification for this token.
        """
        return None

    def resolve(self, settings, msg):
        """
            Resolves this token to ready it for transmission. This means that
            the calculated offsets of actions are fixed.
        """
        return self

    def __repr__(self):
        return self.spec()


class _ValueLiteral(_Token):
    def __init__(self, val):
        self.val = val.decode("string_escape")

    def get_generator(self, settings):
        return generators.LiteralGenerator(self.val)

    def freeze(self, settings):
        return self


class ValueLiteral(_ValueLiteral):
    @classmethod
    def expr(klass):
        e = v_literal.copy()
        return e.setParseAction(klass.parseAction)

    @classmethod
    def parseAction(klass, x):
        v = klass(*x)
        return v

    def spec(self):
        ret = "'%s'"%self.val.encode("string_escape")
        return ret


class ValueNakedLiteral(_ValueLiteral):
    @classmethod
    def expr(klass):
        e = v_naked_literal.copy()
        return e.setParseAction(lambda x: klass(*x))

    def spec(self):
        return self.val.encode("string_escape")


class ValueGenerate(_Token):
    def __init__(self, usize, unit, datatype):
        if not unit:
            unit = "b"
        self.usize, self.unit, self.datatype = usize, unit, datatype

    def bytes(self):
        return self.usize * utils.SIZE_UNITS[self.unit]

    def get_generator(self, settings):
        return generators.RandomGenerator(self.datatype, self.bytes())

    def freeze(self, settings):
        g = self.get_generator(settings)
        return ValueLiteral(g[:].encode("string_escape"))

    @classmethod
    def expr(klass):
        e = pp.Literal("@").suppress() + v_integer

        u = reduce(
            operator.or_,
            [pp.Literal(i) for i in utils.SIZE_UNITS.keys()]
        ).leaveWhitespace()
        e = e + pp.Optional(u, default=None)

        s = pp.Literal(",").suppress()
        s += reduce(
            operator.or_,
            [pp.Literal(i) for i in generators.DATATYPES.keys()]
        )
        e += pp.Optional(s, default="bytes")
        return e.setParseAction(lambda x: klass(*x))

    def spec(self):
        s = "@%s"%self.usize
        if self.unit != "b":
            s += self.unit
        if self.datatype != "bytes":
            s += ",%s"%self.datatype
        return s


class ValueFile(_Token):
    def __init__(self, path):
        self.path = str(path)

    @classmethod
    def expr(klass):
        e = pp.Literal("<").suppress()
        e = e + v_naked_literal
        return e.setParseAction(lambda x: klass(*x))

    def freeze(self, settings):
        return self

    def get_generator(self, settings):
        if not settings.staticdir:
            raise exceptions.FileAccessDenied("File access disabled.")
        s = os.path.expanduser(self.path)
        s = os.path.normpath(
            os.path.abspath(os.path.join(settings.staticdir, s))
        )
        uf = settings.unconstrained_file_access
        if not uf and not s.startswith(settings.staticdir):
            raise exceptions.FileAccessDenied(
                "File access outside of configured directory"
            )
        if not os.path.isfile(s):
            raise exceptions.FileAccessDenied("File not readable")
        return generators.FileGenerator(s)

    def spec(self):
        return "<'%s'"%self.path.encode("string_escape")


Value = pp.MatchFirst(
    [
        ValueGenerate.expr(),
        ValueFile.expr(),
        ValueLiteral.expr()
    ]
)


NakedValue = pp.MatchFirst(
    [
        ValueGenerate.expr(),
        ValueFile.expr(),
        ValueLiteral.expr(),
        ValueNakedLiteral.expr(),
    ]
)


Offset = pp.MatchFirst(
    [
        v_integer,
        pp.Literal("r"),
        pp.Literal("a")
    ]
)


class _Component(_Token):
    """
        A value component of the primary specification of an HTTP message.
    """
    @abc.abstractmethod
    def values(self, settings): # pragma: no cover
        """
           A sequence of value objects.
        """
        return None

    def string(self, settings=None):
        """
            A string representation of the object.
        """
        return "".join(i[:] for i in self.values(settings or {}))


class KeyValue(_Component):
    """
        A key/value pair.
        klass.preamble: leader
    """
    def __init__(self, key, value):
        self.key, self.value = key, value

    @classmethod
    def expr(klass):
        e = pp.Literal(klass.preamble).suppress()
        e += Value
        e += pp.Literal("=").suppress()
        e += Value
        return e.setParseAction(lambda x: klass(*x))

    def spec(self):
        return "%s%s=%s"%(self.preamble, self.key.spec(), self.value.spec())

    def freeze(self, settings):
        return self.__class__(
            self.key.freeze(settings), self.value.freeze(settings)
        )


class PathodSpec(_Token):
    def __init__(self, value):
        self.value = value
        try:
            import http
            self.parsed = http.Response(
                http.Response.expr().parseString(
                    value.val,
                    parseAll=True
                )
            )
        except pp.ParseException, v:
            raise exceptions.ParseException(v.msg, v.line, v.col)

    @classmethod
    def expr(klass):
        e = pp.Literal("s").suppress()
        e = e + ValueLiteral.expr()
        return e.setParseAction(lambda x: klass(*x))

    def values(self, settings):
        return [
            self.value.get_generator(settings),
        ]

    def spec(self):
        return "s%s"%(self.value.spec())

    def freeze(self, settings):
        f = self.parsed.freeze(settings).spec()
        return PathodSpec(ValueLiteral(f.encode("string_escape")))


class SimpleValue(_Component):
    """
        A simple value - i.e. one without a preface.
    """
    def __init__(self, value):
        if isinstance(value, basestring):
            value = ValueLiteral(value)
        self.value = value

    @classmethod
    def expr(klass):
        e = Value | NakedValue
        return e.setParseAction(lambda x: klass(*x))

    def values(self, settings):
        return [
            self.value.get_generator(settings),
        ]

    def spec(self):
        return "%s"%(self.value.spec())

    def freeze(self, settings):
        return self.__class__(self.value.freeze(settings))


class CaselessLiteral(_Component):
    """
        A caseless token that can take only one value.
    """
    def __init__(self, value):
        self.value = value

    @classmethod
    def expr(klass):
        spec = pp.CaselessLiteral(klass.TOK)
        spec = spec.setParseAction(lambda x: klass(*x))
        return spec

    def values(self, settings):
        return self.TOK

    def spec(self):
        return self.TOK

    def freeze(self, settings):
        return self


class OptionsOrValue(_Component):
    """
        Can be any of a specified set of options, or a value specifier.
    """
    preamble = ""
    def __init__(self, value):
        # If it's a string, we were passed one of the options, so we upper-case
        # it to be canonical. The user can specify a different case by using a
        # string value literal.
        self.option_used = False
        if isinstance(value, basestring):
            value = ValueLiteral(value.upper())
            self.option_used = True
        self.value = value

    @classmethod
    def expr(klass):
        parts = [pp.CaselessLiteral(i) for i in klass.options]
        m = pp.MatchFirst(parts)
        spec = m | Value.copy()
        spec = spec.setParseAction(lambda x: klass(*x))
        if klass.preamble:
            spec = pp.Literal(klass.preamble).suppress() + spec
        return spec

    def values(self, settings):
        return [
            self.value.get_generator(settings)
        ]

    def spec(self):
        s = self.value.spec()
        if s[1:-1].lower() in self.options:
            s = s[1:-1].lower()
        return "%s%s"%(self.preamble, s)

    def freeze(self, settings):
        return self.__class__(self.value.freeze(settings))


class Integer(_Component):
    def __init__(self, value):
        self.value = str(value)

    @classmethod
    def expr(klass):
        e = v_integer.copy()
        return e.setParseAction(lambda x: klass(*x))

    def values(self, settings):
        return [generators.LiteralGenerator(self.value)]

    def spec(self):
        return "%s"%(self.value)

    def freeze(self, settings):
        return self


class PreValue(_Component):
    """
        A value lead by self.preamble.
    """
    def __init__(self, value):
        self.value = value

    @classmethod
    def expr(klass):
        e = pp.Literal(klass.preamble).suppress()
        e = e + Value
        return e.setParseAction(lambda x: klass(*x))

    def values(self, settings):
        return [self.value.get_generator(settings)]

    def spec(self):
        return "%s%s"%(self.preamble, self.value.spec())

    def freeze(self, settings):
        return self.__class__(self.value.freeze(settings))


class _Action(_Token):
    """
        An action that operates on the raw data stream of the message. All
        actions have one thing in common: an offset that specifies where the
        action should take place.
    """
    def __init__(self, offset):
        self.offset = offset

    def resolve(self, settings, msg):
        """
            Resolves offset specifications to a numeric offset. Returns a copy
            of the action object.
        """
        c = copy.copy(self)
        l = msg.length(settings)
        if c.offset == "r":
            c.offset = random.randrange(l)
        elif c.offset == "a":
            c.offset = l + 1
        return c

    def __cmp__(self, other):
        return cmp(self.offset, other.offset)

    def __repr__(self):
        return self.spec()

    @abc.abstractmethod
    def spec(self): # pragma: no cover
        pass

    @abc.abstractmethod
    def intermediate(self, settings): # pragma: no cover
        pass


class PauseAt(_Action):
    def __init__(self, offset, seconds):
        _Action.__init__(self, offset)
        self.seconds = seconds

    @classmethod
    def expr(klass):
        e = pp.Literal("p").suppress()
        e += Offset
        e += pp.Literal(",").suppress()
        e += pp.MatchFirst(
            [
                v_integer,
                pp.Literal("f")
            ]
        )
        return e.setParseAction(lambda x: klass(*x))

    def spec(self):
        return "p%s,%s"%(self.offset, self.seconds)

    def intermediate(self, settings):
        return (self.offset, "pause", self.seconds)

    def freeze(self, settings):
        return self


class DisconnectAt(_Action):
    def __init__(self, offset):
        _Action.__init__(self, offset)

    @classmethod
    def expr(klass):
        e = pp.Literal("d").suppress()
        e += Offset
        return e.setParseAction(lambda x: klass(*x))

    def spec(self):
        return "d%s"%self.offset

    def intermediate(self, settings):
        return (self.offset, "disconnect")

    def freeze(self, settings):
        return self


class InjectAt(_Action):
    def __init__(self, offset, value):
        _Action.__init__(self, offset)
        self.value = value

    @classmethod
    def expr(klass):
        e = pp.Literal("i").suppress()
        e += Offset
        e += pp.Literal(",").suppress()
        e += Value
        return e.setParseAction(lambda x: klass(*x))

    def spec(self):
        return "i%s,%s"%(self.offset, self.value.spec())

    def intermediate(self, settings):
        return (
            self.offset,
            "inject",
            self.value.get_generator(settings)
        )

    def freeze(self, settings):
        return InjectAt(self.offset, self.value.freeze(settings))


class _Message(object):
    __metaclass__ = abc.ABCMeta
    logattrs = []

    def __init__(self, tokens):
        self.tokens = tokens

    def toks(self, klass):
        """
            Fetch all tokens that are instances of klass
        """
        return [i for i in self.tokens if isinstance(i, klass)]

    def tok(self, klass):
        """
            Fetch first token that is an instance of klass
        """
        l = self.toks(klass)
        if l:
            return l[0]

    @property
    def actions(self):
        return self.toks(_Action)

    def length(self, settings):
        """
            Calculate the length of the base message without any applied
            actions.
        """
        return sum(len(x) for x in self.values(settings))

    def preview_safe(self):
        """
            Return a copy of this message that issafe for previews.
        """
        tokens = [i for i in self.tokens if not isinstance(i, PauseAt)]
        return self.__class__(tokens)

    def maximum_length(self, settings):
        """
            Calculate the maximum length of the base message with all applied
            actions.
        """
        l = self.length(settings)
        for i in self.actions:
            if isinstance(i, InjectAt):
                l += len(i.value.get_generator(settings))
        return l

    @classmethod
    def expr(klass): # pragma: no cover
        pass

    def log(self, settings):
        """
            A dictionary that should be logged if this message is served.
        """
        ret = {}
        for i in self.logattrs:
            v = getattr(self, i)
            # Careful not to log any VALUE specs without sanitizing them first.
            # We truncate at 1k.
            if hasattr(v, "values"):
                v = [x[:TRUNCATE] for x in v.values(settings)]
                v = "".join(v).encode("string_escape")
            elif hasattr(v, "__len__"):
                v = v[:TRUNCATE]
                v = v.encode("string_escape")
            ret[i] = v
        ret["spec"] = self.spec()
        return ret

    def freeze(self, settings):
        r = self.resolve(settings)
        return self.__class__([i.freeze(settings) for i in r.tokens])

    def __repr__(self):
        return self.spec()
