# -*- Mode: Python -*-
# GObject-Introspection - a framework for introspecting GObject libraries
# Copyright (C) 2008  Johan Dahlin
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the
# Free Software Foundation, Inc., 59 Temple Place - Suite 330,
# Boston, MA 02111-1307, USA.
#

import os
import re

from .ast import (Callback, Enum, Function, Namespace, Member,
                  Parameter, Return, Array, Struct, Field,
                  Type, Alias, Interface, Class, Node, Union,
                  List, Map, Varargs, Constant, type_name_from_ctype,
                  type_names, default_array_types, default_out_types,
                  TYPE_STRING, BASIC_GIR_TYPES, TYPE_NONE)
from .config import DATADIR
from .glibast import GLibBoxed
from .girparser import GIRParser
from .odict import odict
from .sourcescanner import (
    SourceSymbol, ctype_name, CTYPE_POINTER,
    CTYPE_BASIC_TYPE, CTYPE_UNION, CTYPE_ARRAY, CTYPE_TYPEDEF,
    CTYPE_VOID, CTYPE_ENUM, CTYPE_FUNCTION, CTYPE_STRUCT,
    CSYMBOL_TYPE_FUNCTION, CSYMBOL_TYPE_TYPEDEF, CSYMBOL_TYPE_STRUCT,
    CSYMBOL_TYPE_ENUM, CSYMBOL_TYPE_UNION, CSYMBOL_TYPE_OBJECT,
    CSYMBOL_TYPE_MEMBER, CSYMBOL_TYPE_ELLIPSIS, CSYMBOL_TYPE_CONST,
    TYPE_QUALIFIER_CONST)
from .utils import strip_common_prefix, to_underscores

_xdg_data_dirs = [x for x in os.environ.get('XDG_DATA_DIRS', '').split(':') \
                      + [DATADIR, '/usr/share'] if x]


class SkipError(Exception):
    pass


class Names(object):
    names = property(lambda self: self._names)
    aliases = property(lambda self: self._aliases)
    type_names = property(lambda self: self._type_names)
    ctypes = property(lambda self: self._ctypes)

    def __init__(self):
        super(Names, self).__init__()
        self._names = odict() # Maps from GIName -> (namespace, node)
        self._aliases = {} # Maps from GIName -> GIName
        self._type_names = {} # Maps from GTName -> (namespace, node)
        self._ctypes = {} # Maps from CType -> (namespace, node)


class Transformer(object):

    def __init__(self, cachestore, generator,
                 namespace_name, namespace_version):
        self._cachestore = cachestore
        self.generator = generator
        self._namespace = Namespace(namespace_name, namespace_version)
        self._names = Names()
        self._typedefs_ns = {}
        self._strip_prefix = ''
        self._includes = set()
        self._includepaths = []
        self._list_ctypes = []
        self._map_ctypes = []

    def get_names(self):
        return self._names

    def get_includes(self):
        return self._includes

    def set_container_types(self, list_ctypes, map_ctypes):
        self._list_ctypes = list_ctypes
        self._map_ctypes = map_ctypes

    def set_strip_prefix(self, strip_prefix):
        self._strip_prefix = strip_prefix

    def parse(self):
        nodes = []
        for symbol in self.generator.get_symbols():
            node = self._traverse_one(symbol)
            self._add_node(node)
        return self._namespace

    def set_include_paths(self, paths):
        self._includepaths = list(paths)

    def register_include(self, include):
        if include in self._includes:
            return
        filename = self._find_include(include)
        self._parse_include(filename)
        self._includes.add(include)

    # Private

    def _find_include(self, include):
        searchdirs = self._includepaths[:]
        for path in _xdg_data_dirs:
            searchdirs.append(os.path.join(path, 'gir'))

        girname = '%s-%s.gir' % (include.name, include.version)
        for d in searchdirs:
            path = os.path.join(d, girname)
            if os.path.exists(path):
                return path
        else:
            raise ValueError("Couldn't find include %r (search path: %r)"\
                             % (girname, searchdirs))

    def _parse_include(self, filename):
        parser = self._cachestore.load(filename)
        if parser is None:
            parser = GIRParser()
            parser.set_include_parsing(True)
            parser.parse(filename)
            self._cachestore.store(filename, parser)

        for include in parser.get_includes():
            self.register_include(include)

        namespace = parser.get_namespace()
        nsname = namespace.name
        for node in namespace.nodes:
            if isinstance(node, Alias):
                self._names.aliases[node.name] = (nsname, node)
            elif isinstance(node, (GLibBoxed, Interface, Class)):
                self._names.type_names[node.type_name] = (nsname, node)
            giname = '%s.%s' % (nsname, node.name)
            self._names.names[giname] = (nsname, node)
            if hasattr(node, 'ctype'):
                self._names.ctypes[node.ctype] = (nsname, node)
            elif hasattr(node, 'symbol'):
                self._names.ctypes[node.symbol] = (nsname, node)

    def _add_node(self, node):
        if node is None:
            return
        if node.name.startswith('_'):
            return
        self._namespace.nodes.append(node)
        self._names.names[node.name] = (None, node)

    def _strip_namespace_func(self, name):
        prefix = self._namespace.name.lower() + '_'
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
        else:
            prefix = to_underscores(self._namespace.name).lower() + '_'
            if name.lower().startswith(prefix):
                name = name[len(prefix):]
        return self.remove_prefix(name, isfunction=True)

    def remove_prefix(self, name, isfunction=False):
        # when --strip-prefix=g:
        #   GHashTable -> HashTable
        #   g_hash_table_new -> hash_table_new
        prefix = self._strip_prefix.lower()
        if isfunction:
            prefix += '_'
        if len(name) > len(prefix) and name.lower().startswith(prefix):
            name = name[len(prefix):]

        while name.startswith('_'):
            name = name[1:]
        return name

    def _traverse_one(self, symbol, stype=None):
        assert isinstance(symbol, SourceSymbol), symbol

        if stype is None:
            stype = symbol.type
        if stype == CSYMBOL_TYPE_FUNCTION:
            try:
                return self._create_function(symbol)
            except SkipError:
                return
        elif stype == CSYMBOL_TYPE_TYPEDEF:
            return self._create_typedef(symbol)
        elif stype == CSYMBOL_TYPE_STRUCT:
            return self._create_struct(symbol)
        elif stype == CSYMBOL_TYPE_ENUM:
            return self._create_enum(symbol)
        elif stype == CSYMBOL_TYPE_OBJECT:
            return self._create_object(symbol)
        elif stype == CSYMBOL_TYPE_MEMBER:
            return self._create_member(symbol)
        elif stype == CSYMBOL_TYPE_UNION:
            return self._create_union(symbol)
        elif stype == CSYMBOL_TYPE_CONST:
            return self._create_const(symbol)
        else:
            raise NotImplementedError(
                'Transformer: unhandled symbol: %r' % (symbol, ))

    def _create_enum(self, symbol):
        members = []
        for child in symbol.base_type.child_list:
            name = strip_common_prefix(symbol.ident, child.ident).lower()
            members.append(Member(name,
                                  child.const_int,
                                  child.ident))

        enum_name = self.remove_prefix(symbol.ident)
        enum = Enum(enum_name, symbol.ident, members)
        self._names.type_names[symbol.ident] = (None, enum)
        return enum

    def _create_object(self, symbol):
        return Member(symbol.ident, symbol.base_type.name,
                      symbol.ident)

    def _parse_deprecated(self, node, directives):
        deprecated = directives.get('deprecated', False)
        if deprecated:
            deprecated_value = deprecated[0]
            if ':' in deprecated_value:
                # Split out gtk-doc version
                (node.deprecated_version, node.deprecated) = \
                    [x.strip() for x in deprecated_value.split(':', 1)]
            else:
                # No version, just include str
                node.deprecated = deprecated_value.strip()

    def _pair_array(self, params, array):
        if not array.type.length_param_name:
            return
        target_name = array.type.length_param_name
        for i, param in enumerate(params):
            if param.name == array.type.length_param_name:
                array.type.length_param_index = i
                return
        raise ValueError("Unmatched length parameter name %r"\
                             % (target_name, ))

    def _pair_annotations(self, params):
        names = {}
        for param in params:
            if param.name in names:
                raise ValueError("Duplicate parameter name %r"\
                                     % (param.name, ))
            names[param.name] = 1
            if isinstance(param.type, Array):
                self._pair_array(params, param)

    # We take the annotations from the parser as strings; here we
    # want to split them into components, so:
    # (transfer full) -> {'transfer' : [ 'full' ]}

    def _parse_options(self, options):
        ret = {}
        ws_re = re.compile(r'\s+')
        for opt in options:
            items = ws_re.split(opt)
            ret[items[0]] = items[1:]
        return ret

    def _create_function(self, symbol):
        directives = symbol.directives()
        parameters = list(self._create_parameters(
            symbol.base_type, directives))
        self._pair_annotations(parameters)
        return_ = self._create_return(symbol.base_type.base_type,
                                      directives.get('return', {}))
        name = self._strip_namespace_func(symbol.ident)
        func = Function(name, return_, parameters, symbol.ident)
        self._parse_deprecated(func, directives)
        return func

    def _create_source_type(self, source_type):
        if source_type is None:
            return 'None'
        if source_type.type == CTYPE_VOID:
            value = 'void'
        elif source_type.type == CTYPE_BASIC_TYPE:
            value = source_type.name
        elif source_type.type == CTYPE_TYPEDEF:
            value = source_type.name
        elif source_type.type == CTYPE_ARRAY:
            return self._create_source_type(source_type.base_type)
        elif source_type.type == CTYPE_POINTER:
            value = self._create_source_type(source_type.base_type) + '*'
        else:
            value = 'any'
        return value

    def _create_parameters(self, base_type, directives=None):
        if directives is None:
            dirs = {}
        else:
            dirs = directives

        # warn if we see annotations for unknown parameters
        param_names = set(child.ident for child in base_type.child_list)
        dirs_for = set(dirs)
        dirs_for = dirs_for.difference(param_names)
        dirs_for.discard('return')
        if dirs_for:
            print 'Unexpected annotations for %s, parameters are %s' % (
                list(dirs_for), list(param_names), )

        for child in base_type.child_list:
            yield self._create_parameter(
                child, dirs.get(child.ident, {}))

    def _create_member(self, symbol):
        ctype = symbol.base_type.type
        if (ctype == CTYPE_POINTER and
            symbol.base_type.base_type.type == CTYPE_FUNCTION):
            node = self._create_callback(symbol)
        else:
            opts = {}
            if ctype == CTYPE_ARRAY:
                opts['array'] = []
                child_list = list(symbol.base_type.child_list)
                if child_list:
                    size_opt = 'fixed-size=%d' % (child_list[0].const_int, )
                    opts['array'].append(size_opt)
            ftype = self._create_type(symbol.base_type, opts,
                                      is_param=False, is_retval=False)
            ftype = self.resolve_param_type(ftype)
            # Fields are assumed to be read-write
            # (except for Objects, see also glibtransformer.py)
            node = Field(symbol.ident, ftype, symbol.ident,
                       readable=True, writable=True, bits=symbol.const_int)
        return node

    def _create_typedef(self, symbol):
        ctype = symbol.base_type.type
        if (ctype == CTYPE_POINTER and
            symbol.base_type.base_type.type == CTYPE_FUNCTION):
            node = self._create_callback(symbol)
        elif (ctype == CTYPE_POINTER and
            symbol.base_type.base_type.type == CTYPE_STRUCT):
            node = self._create_typedef_struct(symbol, disguised=True)
        elif ctype == CTYPE_STRUCT:
            node = self._create_typedef_struct(symbol)
        elif ctype == CTYPE_UNION:
            node = self._create_typedef_union(symbol)
        elif ctype == CTYPE_ENUM:
            return self._create_enum(symbol)
        elif ctype in (CTYPE_TYPEDEF,
                       CTYPE_POINTER,
                       CTYPE_BASIC_TYPE,
                       CTYPE_VOID):
            name = self.remove_prefix(symbol.ident)
            if symbol.base_type.name:
                target = self.remove_prefix(symbol.base_type.name)
            else:
                target = 'none'
            if name in type_names:
                return None
            return Alias(name, target, ctype=symbol.ident)
        else:
            raise NotImplementedError(
                "symbol %r of type %s" % (symbol.ident, ctype_name(ctype)))
        return node

    def parse_ctype(self, ctype, is_member=False):
        # First look up the ctype including any pointers;
        # a few type names like 'char*' have their own aliases
        # and we need pointer information for those.
        firstpass = type_name_from_ctype(ctype)

        # Remove all pointers - we require standard calling
        # conventions.  For example, an 'int' is always passed by
        # value (unless it's out or inout).
        derefed = firstpass.replace('*', '')

        # Canonicalize our type again, this time without the pointer;
        # this ensures we turn e.g. plain "guint" => "int"
        derefed_typename = type_name_from_ctype(derefed)

        # Preserve "pointerness" of struct/union members
        if (is_member and firstpass.endswith('*') and
            derefed_typename in BASIC_GIR_TYPES):
            return 'any'
        else:
            return derefed_typename

    def _create_type(self, source_type, options, is_param, is_retval):
        ctype = self._create_source_type(source_type)
        if ctype == 'va_list':
            raise SkipError()
        # FIXME: FILE* should not be skipped, it should be handled
        #        properly instead
        elif ctype == 'FILE*':
            raise SkipError

        # Now check for a list/map/array type
        if ctype in self._list_ctypes:
            param = options.get('element-type')
            if param:
                contained_type = self.parse_ctype(param[0])
            else:
                contained_type = None
            derefed_name = self.parse_ctype(ctype)
            rettype = List(derefed_name,
                           ctype,
                           contained_type)
        elif ctype in self._map_ctypes:
            param = options.get('element-type')
            if param:
                key_type = self.parse_ctype(param[0])
                value_type = self.parse_ctype(param[1])
            else:
                key_type = None
                value_type = None
            derefed_name = self.parse_ctype(ctype)
            rettype = Map(derefed_name,
                          ctype,
                          key_type, value_type)
        elif ((is_param and ctype in default_array_types)
              or ('array' in options)):
            derefed_name = ctype[:-1] if ctype[-1] == '*' else ctype
            rettype = Array(ctype,
                            self.parse_ctype(derefed_name))
            array_opts = dict([opt.split('=')
                               for opt in options.get('array', [])])
            if 'length' in array_opts:
                rettype.length_param_name = array_opts['length']
                rettype.zeroterminated = False
            if 'fixed-size' in array_opts:
                rettype.size = array_opts['fixed-size']
                rettype.zeroterminated = False
            if 'zero-terminated' in array_opts:
                rettype.zeroterminated = array_opts['zero-terminated'] != '0'
        else:
            derefed_name = self.parse_ctype(ctype,
                                            not (is_param or is_retval))
            rettype = Type(derefed_name, ctype)

        # Deduce direction for some types passed by reference that
        # aren't arrays; modifies the options array.
        if ('array' not in options and
            not ('out' in options or
                 'in' in options or
                 'inout' in options or
                 'in-out' in options) and
            source_type.type == CTYPE_POINTER and
            derefed_name in default_out_types):
            options['out'] = []

        if 'transfer' in options:
            # Transfer is specified, we don't question it.
            return rettype

        canontype = type_name_from_ctype(ctype)

        # Since no transfer is specified, we drop into a bunch of
        # heuristics to guess it.  This mutates the options array to
        # set the 'transfer' option.
        # Note that we inferred the transfer
        options['transfer-inferred'] = []
        stype = source_type
        if canontype == TYPE_STRING:
            # It's a string - we just look at 'const'
            if source_type.base_type.type_qualifier & TYPE_QUALIFIER_CONST:
                options['transfer'] = ['none']
            else:
                options['transfer'] = ['full']
        elif 'array' in options or stype.type == CTYPE_ARRAY:
            # It's rare to mutate arrays in public GObject APIs
            options['transfer'] = ['none']
        elif (canontype in BASIC_GIR_TYPES or
              canontype == TYPE_NONE or
              stype.type == CTYPE_ENUM):
            # Basic types default to 'none'
            options['transfer'] = ['none']
        elif (stype.type == CTYPE_POINTER and
              stype.base_type.type_qualifier & TYPE_QUALIFIER_CONST):
            # Anything with 'const' gets none
            options['transfer'] = ['none']
        elif is_param and stype.type == CTYPE_POINTER:
            # For generic pointer types, let's look at the argument
            # direction.  An out/inout argument gets full, everything
            # else none.
            if ('out' in options or
                'inout' in options or
                'in-out' in options):
                options['transfer'] = ['full']
            else:
                options['transfer'] = ['none']
        else:
            # For anything else we default to none for parameters;
            # this covers enums and possibly some other corner cases.
            # Return values of structures and the like will end up
            # full.
            if is_param:
                options['transfer'] = ['none']
            else:
                options['transfer'] = ['full']

        return rettype

    def _handle_generic_param_options(self, param, options):
        for option, data in options.iteritems():
            if option == 'transfer':
                if data:
                    depth = data[0]
                    if depth not in ('none', 'container', 'full'):
                        raise ValueError("Invalid transfer %r" % (depth, ))
                else:
                    depth = 'full'
                param.transfer = depth
            elif option == 'transfer-inferred':
                # This is a purely internal flag; we don't expect
                # people to write it
                param.transfer_inferred = True

    def _create_parameter(self, symbol, options):
        options = self._parse_options(options)
        if symbol.type == CSYMBOL_TYPE_ELLIPSIS:
            ptype = Varargs()
            if 'transfer' not in options:
                options['transfer'] = ['none']
        else:
            ptype = self._create_type(symbol.base_type, options,
                                      is_param=True, is_retval=False)
            ptype = self.resolve_param_type(ptype)
        param = Parameter(symbol.ident, ptype)
        for option, data in options.iteritems():
            if option in ['in-out', 'inout']:
                param.direction = 'inout'
            elif option == 'in':
                param.direction = 'in'
            elif option == 'out':
                param.direction = 'out'
            elif option == 'allow-none':
                param.allow_none = True
            elif option.startswith(('element-type', 'array')):
                pass
            elif option in ('transfer', 'transfer-inferred'):
                pass
            else:
                print 'Unhandled parameter annotation option: %r' % (
                    option, )
        self._handle_generic_param_options(param, options)

        assert param.transfer is not None, param
        return param

    def _create_return(self, source_type, options=None):
        if options is None:
            options_map = {}
        else:
            options_map = self._parse_options(options)
        rtype = self._create_type(source_type, options_map,
                                  is_param=False, is_retval=True)
        rtype = self.resolve_param_type(rtype)
        return_ = Return(rtype)
        self._handle_generic_param_options(return_, options_map)
        for option, data in options_map.iteritems():
            if option in ('transfer', 'transfer-inferred',
                          'element-type', 'out'):
                pass
            else:
                print 'Unhandled return type annotation option: %r' % (
                    option, )

        assert return_.transfer is not None, return_
        return return_

    def _create_const(self, symbol):
        name = self.remove_prefix(symbol.ident)
        if symbol.const_string is None:
            type_name = 'int'
            value = symbol.const_int
        else:
            type_name = 'utf8'
            value = symbol.const_string
        const = Constant(name, type_name, value)
        return const

    def _create_typedef_struct(self, symbol, disguised=False):
        name = self.remove_prefix(symbol.ident)
        struct = Struct(name, symbol.ident, disguised)
        self._typedefs_ns[symbol.ident] = struct
        self._create_struct(symbol)
        return struct

    def _create_typedef_union(self, symbol):
        name = self.remove_prefix(symbol.ident)
        union = Union(name, symbol.ident)
        self._typedefs_ns[symbol.ident] = union
        self._create_union(symbol)
        return union

    def _create_struct(self, symbol):
        struct = self._typedefs_ns.get(symbol.ident, None)
        if struct is None:
            # This is a bit of a hack; really we should try
            # to resolve through the typedefs to find the real
            # name
            if symbol.ident.startswith('_'):
                name = symbol.ident[1:]
            else:
                name = symbol.ident
            name = self.remove_prefix(name)
            struct = Struct(name, symbol.ident)

        for child in symbol.base_type.child_list:
            field = self._traverse_one(child)
            if field:
                struct.fields.append(field)

        return struct

    def _create_union(self, symbol):
        union = self._typedefs_ns.get(symbol.ident, None)
        if union is None:
            # This is a bit of a hack; really we should try
            # to resolve through the typedefs to find the real
            # name
            if symbol.ident.startswith('_'):
                name = symbol.ident[1:]
            else:
                name = symbol.ident
            name = self.remove_prefix(name)
            union = Union(name, symbol.ident)

        for child in symbol.base_type.child_list:
            field = self._traverse_one(child)
            if field:
                union.fields.append(field)

        return union

    def _create_callback(self, symbol):
        directives = symbol.directives()
        parameters = self._create_parameters(symbol.base_type.base_type,
            directives)
        retval = self._create_return(symbol.base_type.base_type.base_type,
            directives.get('return', {}))
        if symbol.ident.find('_') > 0:
            name = self.remove_prefix(symbol.ident, True)
        else:
            name = self.remove_prefix(symbol.ident)
        return Callback(name, retval, list(parameters), symbol.ident)

    def _typepair_to_str(self, item):
        nsname, item = item
        if nsname is None:
            return item.name
        return '%s.%s' % (nsname, item.name)

    def _resolve_type_name_1(self, type_name, ctype, names):
        # First look using the built-in names
        if ctype:
            try:
                return type_names[ctype]
            except KeyError, e:
                pass
        try:
            return type_names[type_name]
        except KeyError, e:
            pass

        if ctype:
            ctype = ctype.replace('*', '')
            resolved = names.ctypes.get(ctype)
            if resolved:
                return self._typepair_to_str(resolved)
        type_name = self.remove_prefix(type_name)
        resolved = names.aliases.get(type_name)
        if resolved:
            return self._typepair_to_str(resolved)
        resolved = names.names.get(type_name)
        if resolved:
            return self._typepair_to_str(resolved)
        resolved = names.type_names.get(type_name)
        if resolved:
            return self._typepair_to_str(resolved)
        raise KeyError("failed to find %r" % (type_name, ))

    def resolve_type_name_full(self, type_name, ctype,
                               names, allow_invalid=True):
        try:
            return self._resolve_type_name_1(type_name, ctype, names)
        except KeyError, e:
            try:
                return self._resolve_type_name_1(type_name, ctype, self._names)
            except KeyError, e:
                if not allow_invalid:
                    raise
                return type_name

    def resolve_type_name(self, type_name, ctype=None):
        try:
            return self.resolve_type_name_full(type_name, ctype, self._names)
        except KeyError, e:
            return type_name

    def gtypename_to_giname(self, gtname, names):
        resolved = names.type_names.get(gtname)
        if resolved:
            return self._typepair_to_str(resolved)
        resolved = self._names.type_names.get(gtname)
        if resolved:
            return self._typepair_to_str(resolved)
        raise KeyError("Failed to resolve GType name: %r" % (gtname, ))

    def ctype_of(self, obj):
        if hasattr(obj, 'ctype'):
            return obj.ctype
        elif hasattr(obj, 'symbol'):
            return obj.symbol
        else:
            return None

    def resolve_param_type_full(self, ptype, names, **kwargs):
        if isinstance(ptype, Node):
            ptype.name = self.resolve_type_name_full(ptype.name,
                                                     self.ctype_of(ptype),
                                                     names, **kwargs)
            if isinstance(ptype, (Array, List)):
                if ptype.element_type is not None:
                    ptype.element_type = \
                        self.resolve_param_type_full(ptype.element_type,
                                                     names, **kwargs)
            if isinstance(ptype, Map):
                if ptype.key_type is not None:
                    ptype.key_type = \
                        self.resolve_param_type_full(ptype.key_type,
                                                     names, **kwargs)
                    ptype.value_type = \
                        self.resolve_param_type_full(ptype.value_type,
                                                     names, **kwargs)
        elif isinstance(ptype, basestring):
            return self.resolve_type_name_full(ptype, None, names, **kwargs)
        else:
            raise AssertionError("Unhandled param: %r" % (ptype, ))
        return ptype

    def resolve_param_type(self, ptype):
        try:
            return self.resolve_param_type_full(ptype, self._names)
        except KeyError, e:
            return ptype

    def follow_aliases(self, type_name, names):
        while True:
            resolved = names.aliases.get(type_name)
            if resolved:
                (ns, alias) = resolved
                type_name = alias.target
            else:
                break
        return type_name
