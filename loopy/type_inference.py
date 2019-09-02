from __future__ import division, absolute_import

__copyright__ = "Copyright (C) 2012-16 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import six

from pymbolic.mapper import CombineMapper
import numpy as np

from loopy.tools import is_integer
from loopy.types import NumpyType

from loopy.diagnostic import (
        LoopyError,
        TypeInferenceFailure, DependencyTypeInferenceFailure)
from loopy.kernel.instruction import _DataObliviousInstruction

from loopy.program import CallablesTable
from loopy.symbolic import (
        LinearSubscript, parse_tagged_name, RuleAwareIdentityMapper,
        SubstitutionRuleExpander, ResolvedFunction,
        SubstitutionRuleMappingContext, SubArrayRef)
from pymbolic.primitives import Variable, Subscript, Lookup

import logging
logger = logging.getLogger(__name__)


def _debug(kernel, s, *args):
    if logger.isEnabledFor(logging.DEBUG):
        logstr = s % args
        logger.debug("%s: %s" % (kernel.name, logstr))


def get_return_types_as_tuple(arg_id_to_dtype):
    """Returns the types of arguments in  a tuple format.

    :arg arg_id_to_dtype: An instance of :class:`dict` which denotes a
                            mapping from the arguments to their inferred types.
    """
    return_arg_id_to_dtype = dict((id, dtype) for id, dtype in
            arg_id_to_dtype.items() if (isinstance(id, int) and id < 0))
    return_arg_pos = sorted(return_arg_id_to_dtype.keys(), reverse=True)

    return tuple(return_arg_id_to_dtype[id] for id in return_arg_pos)


# {{{ renaming helpers

class FunctionNameChanger(RuleAwareIdentityMapper):
    """
    Changes the names of scoped functions in calls of expressions according to
    the mapping ``calls_to_new_functions``
    """

    def __init__(self, rule_mapping_context, calls_to_new_names,
            subst_expander):
        super(FunctionNameChanger, self).__init__(rule_mapping_context)
        self.calls_to_new_names = calls_to_new_names
        self.subst_expander = subst_expander

    def map_call(self, expr, expn_state):
        name, tag = parse_tagged_name(expr.function)

        if name not in self.rule_mapping_context.old_subst_rules:
            expanded_expr = self.subst_expander(expr)
            if expr in self.calls_to_new_names:
                return type(expr)(
                        ResolvedFunction(self.calls_to_new_names[expr]),
                        tuple(self.rec(child, expn_state)
                            for child in expr.parameters))
            elif expanded_expr in self.calls_to_new_names:
                # FIXME: This is killing the substitution.
                # Maybe using a RuleAwareIdentityMapper for TypeInferenceMapper
                # would help.
                return type(expr)(
                        ResolvedFunction(self.calls_to_new_names[expanded_expr]),
                        tuple(self.rec(child, expn_state)
                            for child in expanded_expr.parameters))
            else:
                return super(FunctionNameChanger, self).map_call(
                        expr, expn_state)
        else:
            return self.map_substitution(name, tag, expr.parameters, expn_state)

    def map_call_with_kwargs(self, expr, expn_state):

        if expr in self.calls_to_new_names:
            return type(expr)(
                ResolvedFunction(self.calls_to_new_names[expr]),
                tuple(self.rec(child, expn_state)
                    for child in expr.parameters),
                dict(
                    (key, self.rec(val, expn_state))
                    for key, val in six.iteritems(expr.kw_parameters))
                    )
        else:
            return super(FunctionNameChanger, self).map_call_with_kwargs(
                    expr, expn_state)


def change_names_of_pymbolic_calls(kernel, pymbolic_calls_to_new_names):
    """
    Returns a copy of *kernel* with the names of pymbolic calls changed
    according to the mapping given by *pymbolic_calls_new_names*.

    :arg pymbolic_calls_to_new_names: A mapping from instances of
        :class:`pymbolic.primitives.Call` to :class:`str`.

    **Example: **

        - Given a *kernel* --

        .. code::

            -------------------------------------------------------------
            KERNEL: loopy_kernel
            -------------------------------------------------------------
            ARGUMENTS:
            x: type: <auto/runtime>, shape: (10), dim_tags: (N0:stride:1)
            y: type: <auto/runtime>, shape: (10), dim_tags: (N0:stride:1)
            -------------------------------------------------------------
            DOMAINS:
            { [i] : 0 <= i <= 9 }
            -------------------------------------------------------------
            INAME IMPLEMENTATION TAGS:
            i: None
            -------------------------------------------------------------
            INSTRUCTIONS:
            for i
                y[i] = ResolvedFunction('sin')(x[i])
            end i
            -------------------------------------------------------------

        - And given a *pymbolic_calls_to_new_names* --

        .. code::

            {Call(ResolvedFunction(Variable('sin')), (Subscript(Variable('x'),
            Variable('i')),))": 'sin_1'}

        - The following *kernel* is returned --

        .. code::

            -------------------------------------------------------------
            KERNEL: loopy_kernel
            -------------------------------------------------------------
            ARGUMENTS:
            x: type: <auto/runtime>, shape: (10), dim_tags: (N0:stride:1)
            y: type: <auto/runtime>, shape: (10), dim_tags: (N0:stride:1)
            -------------------------------------------------------------
            DOMAINS:
            { [i] : 0 <= i <= 9 }
            -------------------------------------------------------------
            INAME IMPLEMENTATION TAGS:
            i: None
            -------------------------------------------------------------
            INSTRUCTIONS:
            for i
                y[i] = ResolvedFunction('sin_1')(x[i])
            end i
            -------------------------------------------------------------
    """
    rule_mapping_context = SubstitutionRuleMappingContext(
                    kernel.substitutions, kernel.get_var_name_generator())
    subst_expander = SubstitutionRuleExpander(kernel.substitutions)
    name_changer = FunctionNameChanger(rule_mapping_context,
            pymbolic_calls_to_new_names, subst_expander)

    return rule_mapping_context.finish_kernel(
            name_changer.map_kernel(kernel))

# }}}


# {{{ type inference mapper

class TypeInferenceMapper(CombineMapper):
    def __init__(self, kernel, callables_table, new_assignments=None):
        """
        :arg new_assignments: mapping from names to either
            :class:`loopy.kernel.data.TemporaryVariable`
            or
            :class:`loopy.kernel.data.KernelArgument`
            instances
        """
        self.kernel = kernel
        assert isinstance(callables_table, CallablesTable)
        if new_assignments is None:
            new_assignments = {}
        self.new_assignments = new_assignments
        self.symbols_with_unknown_types = set()
        self.callables_table = callables_table
        self.old_calls_to_new_calls = {}

    def __call__(self, expr, return_tuple=False, return_dtype_set=False):
        kwargs = {}
        if return_tuple:
            kwargs["return_tuple"] = True

        result = super(TypeInferenceMapper, self).__call__(
                expr, **kwargs)

        assert isinstance(result, list)

        if return_tuple:
            for result_i in result:
                assert isinstance(result_i, tuple)

            assert return_dtype_set
            return result

        else:
            if return_dtype_set:
                return result
            else:
                if not result:
                    raise DependencyTypeInferenceFailure(
                            ", ".join(sorted(self.symbols_with_unknown_types)))

                result, = result
                return result

    # /!\ Introduce caches with care--numpy.float32(x) and numpy.float64(x)
    # are Python-equal (for many common constants such as integers).

    def copy(self, callables_table=None):
        if callables_table is None:
            callables_table = self.callables_table
        return type(self)(self.kernel, callables_table,
                self.new_assignments)

    def with_assignments(self, names_to_vars):
        new_ass = self.new_assignments.copy()
        new_ass.update(names_to_vars)
        return type(self)(self.kernel, self.callables_table, new_ass)

    @staticmethod
    def combine(dtype_sets):
        """
        :arg dtype_sets: A list of lists, where each of the inner lists
            consists of either zero or one type. An empty list is
            consistent with any type. A list with a type requires
            that an operation be valid in conjunction with that type.
        """
        dtype_sets = list(dtype_sets)

        from loopy.types import LoopyType, NumpyType
        assert all(
                all(isinstance(dtype, LoopyType) for dtype in dtype_set)
                for dtype_set in dtype_sets)
        assert all(
                0 <= len(dtype_set) <= 1
                for dtype_set in dtype_sets)

        from pytools import is_single_valued

        dtypes = [dtype
                for dtype_set in dtype_sets
                for dtype in dtype_set]

        if not all(isinstance(dtype, NumpyType) for dtype in dtypes):
            if not is_single_valued(dtypes):
                raise TypeInferenceFailure(
                        "Nothing known about operations between '%s'"
                        % ", ".join(str(dtype) for dtype in dtypes))

            return [dtypes[0]]

        numpy_dtypes = [dtype.dtype for dtype in dtypes]

        if not numpy_dtypes:
            return []

        if is_single_valued(numpy_dtypes):
            return [dtypes[0]]

        result = numpy_dtypes.pop()
        while numpy_dtypes:
            other = numpy_dtypes.pop()

            if result.fields is None and other.fields is None:
                if (result, other) in [
                        (np.int32, np.float32), (np.float32, np.int32)]:
                    # numpy makes this a double. I disagree.
                    result = np.dtype(np.float32)
                else:
                    result = (
                            np.empty(0, dtype=result)
                            + np.empty(0, dtype=other)
                            ).dtype

            elif result.fields is None and other.fields is not None:
                # assume the non-native type takes over
                # (This is used for vector types.)
                result = other
            elif result.fields is not None and other.fields is None:
                # assume the non-native type takes over
                # (This is used for vector types.)
                pass
            else:
                if result is not other:
                    raise TypeInferenceFailure(
                            "nothing known about result of operation on "
                            "'%s' and '%s'" % (result, other))

        return [NumpyType(result)]

    def map_sum(self, expr):
        dtype_sets = []
        small_integer_dtype_sets = []
        for child in expr.children:
            dtype_set = self.rec(child)
            if is_integer(child) and abs(child) < 1024:
                small_integer_dtype_sets.append(dtype_set)
            else:
                dtype_sets.append(dtype_set)

        if all(dtype.is_integral()
                for dtype_set in dtype_sets
                for dtype in dtype_set):
            dtype_sets.extend(small_integer_dtype_sets)

        return self.combine(dtype_sets)

    map_product = map_sum

    def map_quotient(self, expr):
        n_dtype_set = self.rec(expr.numerator)
        d_dtype_set = self.rec(expr.denominator)

        dtypes = n_dtype_set + d_dtype_set

        if all(dtype.is_integral() for dtype in dtypes):
            # both integers
            return [NumpyType(np.dtype(np.float64))]

        else:
            return self.combine([n_dtype_set, d_dtype_set])

    def map_constant(self, expr):
        if is_integer(expr):
            for tp in [np.int32, np.int64]:
                iinfo = np.iinfo(tp)
                if iinfo.min <= expr <= iinfo.max:
                    return [NumpyType(np.dtype(tp))]

            else:
                raise TypeInferenceFailure("integer constant '%s' too large" % expr)

        dt = np.asarray(expr).dtype
        if hasattr(expr, "dtype"):
            return [NumpyType(expr.dtype)]
        elif isinstance(expr, np.number):
            # Numpy types are sized
            return [NumpyType(np.dtype(type(expr)))]
        elif dt.kind == "f":
            # deduce the smaller type by default
            return [NumpyType(np.dtype(np.float32))]
        elif dt.kind == "c":
            if np.complex64(expr) == np.complex128(expr):
                # (COMPLEX_GUESS_LOGIC)
                # No precision is lost by 'guessing' single precision, use that.
                # This at least covers simple cases like '1j'.
                return [NumpyType(np.dtype(np.complex64))]

            # Codegen for complex types depends on exactly correct types.
            # Refuse temptation to guess.
            raise TypeInferenceFailure("Complex constant '%s' needs to "
                    "be sized (i.e. as numpy.complex64/128) for type inference "
                    % expr)
        else:
            raise TypeInferenceFailure("Cannot deduce type of constant '%s'" % expr)

    def map_type_cast(self, expr):
        subtype, = self.rec(expr.child)
        if not issubclass(subtype.dtype.type, np.number):
            raise LoopyError("Can't cast a '%s' to '%s'" % (subtype, expr.type))
        return [expr.type]

    def map_subscript(self, expr):
        return self.rec(expr.aggregate)

    def map_linear_subscript(self, expr):
        return self.rec(expr.aggregate)

    def map_call(self, expr, return_tuple=False):

        from pymbolic.primitives import Variable, CallWithKwargs, Call

        if isinstance(expr, CallWithKwargs):
            kw_parameters = expr.kw_parameters
        else:
            assert isinstance(expr, Call)
            kw_parameters = {}

        identifier = expr.function
        if isinstance(identifier, (Variable, ResolvedFunction)):
            identifier = identifier.name

        def none_if_empty(d):
            if d:
                d, = d
                return d
            else:
                return None

        arg_id_to_dtype = dict((i, none_if_empty(self.rec(par))) for (i, par) in
                tuple(enumerate(expr.parameters)) + tuple(kw_parameters.items()))

        # specializing the known function wrt type
        if isinstance(expr.function, ResolvedFunction):
            in_knl_callable = self.callables_table[expr.function.name]

            # {{{ checking that there is no overwriting of types of in_knl_callable

            if in_knl_callable.arg_id_to_dtype is not None:

                # specializing an already specialized function.
                for id, dtype in arg_id_to_dtype.items():
                    if id in in_knl_callable.arg_id_to_dtype and (
                            in_knl_callable.arg_id_to_dtype[id] !=
                            arg_id_to_dtype[id]):

                        # {{{ ignoring the the cases when there is a discrepancy
                        # between np.uint and np.int

                        import numpy as np
                        if in_knl_callable.arg_id_to_dtype[id].dtype.type == (
                                np.uint32) and (
                                        arg_id_to_dtype[id].dtype.type == np.int32):
                            continue
                        if in_knl_callable.arg_id_to_dtype[id].dtype.type == (
                                np.uint64) and (
                                        arg_id_to_dtype[id].dtype.type ==
                                        np.int64):
                            continue

                        if np.can_cast(arg_id_to_dtype[id].dtype.type,
                                in_knl_callable.arg_id_to_dtype[id].dtype.type):
                            continue

                        # }}}

                        raise LoopyError("Overwriting a specialized function "
                                "is illegal--maybe start with new instance of "
                                "InKernelCallable?")

            # }}}
            in_knl_callable, self.callables_table = (
                    in_knl_callable.with_types(
                        arg_id_to_dtype, self.kernel,
                        self.callables_table))

            in_knl_callable = in_knl_callable.with_target(self.kernel.target)

            # storing the type specialized function so that it can be used for
            # later use
            self.callables_table, new_function_id = (
                    self.callables_table.with_callable(
                        expr.function.function,
                        in_knl_callable))

            if isinstance(expr, Call):
                self.old_calls_to_new_calls[expr] = new_function_id
            else:
                assert isinstance(expr, CallWithKwargs)
                self.old_calls_to_new_calls[expr] = new_function_id

            new_arg_id_to_dtype = in_knl_callable.arg_id_to_dtype

            if new_arg_id_to_dtype is None:
                return []

            # collecting result dtypes in order of the assignees
            if -1 in new_arg_id_to_dtype and new_arg_id_to_dtype[-1] is not None:
                if return_tuple:
                    return [get_return_types_as_tuple(new_arg_id_to_dtype)]
                else:
                    return [new_arg_id_to_dtype[-1]]

        elif isinstance(expr.function, Variable):
            # Since, the function is not "scoped", attempt to infer using
            # kernel.function_manglers

            # {{{ trying to infer using function manglers

            arg_dtypes = tuple(none_if_empty(self.rec(par)) for par in
                    expr.parameters)

            # finding the function_mangler which would be associated with the
            # realized function.

            mangle_result = None
            for function_mangler in self.kernel.function_manglers:
                mangle_result = function_mangler(self.kernel, identifier,
                        arg_dtypes)
                if mangle_result:
                    # found a match.
                    break

            if mangle_result is not None:
                from loopy.kernel.function_interface import (ManglerCallable,
                        ValueArgDescriptor)

                # creating arg_id_to_dtype, arg_id_to_descr from arg_dtypes
                arg_id_to_dtype = dict((i, dt.with_target(self.kernel.target))
                        for i, dt in enumerate(mangle_result.arg_dtypes))
                arg_id_to_dtype.update(dict((-i-1,
                    dtype.with_target(self.kernel.target)) for i, dtype in enumerate(
                        mangle_result.result_dtypes)))
                arg_descrs = tuple((i, ValueArgDescriptor()) for i, _ in
                        enumerate(mangle_result.arg_dtypes))
                res_descrs = tuple((-i-1, ValueArgDescriptor()) for i, _ in
                        enumerate(mangle_result.result_dtypes))
                arg_id_to_descr = dict(arg_descrs+res_descrs)

                # creating the ManglerCallable object corresponding to the
                # function.
                in_knl_callable = ManglerCallable(
                        identifier, function_mangler, arg_id_to_dtype,
                        arg_id_to_descr, mangle_result.target_name)
                self.callables_table, new_function_id = (
                        self.callables_table.with_added_callable(
                            expr.function, in_knl_callable))

                if isinstance(expr, Call):
                    self.old_calls_to_new_calls[expr] = new_function_id
                else:
                    assert isinstance(expr, CallWithKwargs)
                    self.old_calls_to_new_calls = new_function_id

            # Returning the type.
            if return_tuple:
                if mangle_result is not None:
                    return [mangle_result.result_dtypes]
            else:
                if mangle_result is not None:
                    if len(mangle_result.result_dtypes) != 1 and not return_tuple:
                        raise LoopyError("functions with more or fewer than one "
                                "return value may only be used in direct "
                                "assignments")

                    return [mangle_result.result_dtypes[0]]
            # }}}

        return []

    map_call_with_kwargs = map_call

    def map_variable(self, expr):
        if expr.name in self.kernel.all_inames():
            return [self.kernel.index_dtype]

        result = self.kernel.mangle_symbol(
                self.kernel.target.get_device_ast_builder(),
                expr.name)

        if result is not None:
            result_dtype, _ = result
            return [result_dtype]

        obj = self.new_assignments.get(expr.name)

        if obj is None:
            obj = self.kernel.arg_dict.get(expr.name)

        if obj is None:
            obj = self.kernel.temporary_variables.get(expr.name)

        if obj is None:
            raise TypeInferenceFailure("name not known in type inference: %s"
                    % expr.name)

        from loopy.kernel.data import TemporaryVariable, KernelArgument
        import loopy as lp
        if isinstance(obj, (KernelArgument, TemporaryVariable)):
            assert obj.dtype is not lp.auto
            result = [obj.dtype]
            if result[0] is None:
                self.symbols_with_unknown_types.add(expr.name)
                return []
            else:
                return result

        else:
            raise RuntimeError("unexpected type inference "
                    "object type for '%s'" % expr.name)

    map_tagged_variable = map_variable

    def map_lookup(self, expr):
        agg_result = self.rec(expr.aggregate)
        if not agg_result:
            return agg_result

        numpy_dtype = agg_result[0].numpy_dtype
        fields = numpy_dtype.fields
        if fields is None:
            raise LoopyError("cannot look up attribute '%s' in "
                    "non-aggregate expression '%s'"
                    % (expr.name, expr.aggregate))

        try:
            field = fields[expr.name]
        except KeyError:
            raise LoopyError("cannot look up attribute '%s' in "
                    "aggregate expression '%s' of dtype '%s'"
                    % (expr.aggregate, expr.name, numpy_dtype))

        dtype = field[0]
        return [NumpyType(dtype)]

    def map_comparison(self, expr):
        # "bool" is unusable because OpenCL's bool has indeterminate memory
        # format.
        self(expr.left, return_tuple=False, return_dtype_set=False)
        self(expr.right, return_tuple=False, return_dtype_set=False)
        return [NumpyType(np.dtype(np.int32))]

    def map_logical_not(self, expr):
        return [NumpyType(np.dtype(np.int32))]

    def map_logical_and(self, expr):
        for child in expr.children:
            self.rec(child)

        return [NumpyType(np.dtype(np.int32))]

    map_logical_or = map_logical_and

    def map_group_hw_index(self, expr, *args):
        return [self.kernel.index_dtype]

    def map_local_hw_index(self, expr, *args):
        return [self.kernel.index_dtype]

    def map_reduction(self, expr, return_tuple=False):
        """
        :arg return_tuple: If *True*, treat the reduction as having tuple type.
        Otherwise, if *False*, the reduction must have scalar type.
        """
        from loopy.symbolic import Reduction
        from pymbolic.primitives import Call

        if not return_tuple and expr.is_tuple_typed:
            raise LoopyError("reductions with more or fewer than one "
                             "return value may only be used in direct "
                             "assignments")

        if isinstance(expr.expr, tuple):
            rec_results = [self.rec(sub_expr) for sub_expr in expr.expr]
            from itertools import product
            rec_results = product(*rec_results)
        elif isinstance(expr.expr, Reduction):
            rec_results = self.rec(expr.expr, return_tuple=return_tuple)
        elif isinstance(expr.expr, Call):
            rec_results = self.map_call(expr.expr, return_tuple=return_tuple)
        else:
            if return_tuple:
                raise LoopyError("unknown reduction type for tuple reduction: '%s'"
                        % type(expr.expr).__name__)
            else:
                rec_results = self.rec(expr.expr)

        if return_tuple:
            return [expr.operation.result_dtypes(self.kernel, *rec_result)
                    for rec_result in rec_results]
        else:
            return [expr.operation.result_dtypes(self.kernel, rec_result)[0]
                    for rec_result in rec_results]

    def map_sub_array_ref(self, expr):
        return self.rec(expr.get_begin_subscript())


# }}}


# {{{ infer single variable

def _infer_var_type(kernel, var_name, type_inf_mapper, subst_expander):

    if var_name in kernel.all_params():
        return [kernel.index_dtype], [], {}, (
                type_inf_mapper.callables_table)

    from functools import partial
    debug = partial(_debug, kernel)

    dtype_sets = []

    import loopy as lp

    type_inf_mapper = type_inf_mapper.copy()

    for writer_insn_id in kernel.writer_map().get(var_name, []):
        writer_insn = kernel.id_to_insn[writer_insn_id]
        if not isinstance(writer_insn, lp.MultiAssignmentBase):
            continue

        expr = subst_expander(writer_insn.expression)

        debug("             via expr %s", expr)
        if isinstance(writer_insn, lp.Assignment):
            result = type_inf_mapper(expr, return_dtype_set=True)
        elif isinstance(writer_insn, lp.CallInstruction):
            # FIXME: Unnecessary separation of logic between CallInstruction
            # and Assignment.
            return_dtype_set = type_inf_mapper(expr,
                    return_tuple=len(writer_insn.assignees) != 1,
                    return_dtype_set=True)

            if len(writer_insn.assignees) == 1:
                return_dtype_set = (return_dtype_set, )

            result = []
            for return_dtype_set in return_dtype_set:
                result_i = None
                found = False
                for assignee, comp_dtype_set in zip(
                        writer_insn.assignee_var_names(), return_dtype_set):
                    if assignee == var_name:
                        found = True
                        result_i = comp_dtype_set
                        break

                assert found
                if result_i is not None:
                    result.append(result_i)

        debug("             result: %s", result)

        dtype_sets.append(result)

    if not dtype_sets:
        return (
                None, type_inf_mapper.symbols_with_unknown_types, None,
                type_inf_mapper.callables_table)

    result = type_inf_mapper.combine(dtype_sets)

    return (result, type_inf_mapper.symbols_with_unknown_types,
            type_inf_mapper.old_calls_to_new_calls,
            type_inf_mapper.callables_table)

# }}}


class _DictUnionView:
    def __init__(self, children):
        self.children = children

    def get(self, key):
        try:
            return self[key]
        except KeyError:
            return None

    def __getitem__(self, key):
        for ch in self.children:
            try:
                return ch[key]
            except KeyError:
                pass

        raise KeyError(key)


# {{{ infer_unknown_types

def infer_unknown_types_for_a_single_kernel(kernel, callables_table,
        expect_completion=False):
    """Infer types on temporaries and arguments."""

    logger.debug("%s: infer types" % kernel.name)

    from functools import partial
    debug = partial(_debug, kernel)

    import time
    start_time = time.time()

    unexpanded_kernel = kernel
    if kernel.substitutions:
        from loopy.transform.subst import expand_subst
        kernel = expand_subst(kernel)

    new_temp_vars = kernel.temporary_variables.copy()
    new_arg_dict = kernel.arg_dict.copy()

    # {{{ find names_with_unknown_types

    # contains both arguments and temporaries
    names_for_type_inference = []

    import loopy as lp
    for tv in six.itervalues(kernel.temporary_variables):
        assert tv.dtype is not lp.auto
        if tv.dtype is None:
            names_for_type_inference.append(tv.name)

    for arg in kernel.args:
        assert arg.dtype is not lp.auto
        if arg.dtype is None:
            names_for_type_inference.append(arg.name)

    # }}}

    logger.debug("finding types for {count:d} names".format(
            count=len(names_for_type_inference)))

    writer_map = kernel.writer_map()

    dep_graph = dict(
            (written_var, set(
                read_var
                for insn_id in writer_map.get(written_var, [])
                for read_var in kernel.id_to_insn[insn_id].read_dependency_names()
                if read_var in names_for_type_inference))
            for written_var in names_for_type_inference)

    from loopy.tools import compute_sccs

    # To speed up processing, we sort the variables by computing the SCCs of the
    # type dependency graph. Each SCC represents a set of variables whose types
    # mutually depend on themselves. The SCCs are returned and processed in
    # topological order.
    sccs = compute_sccs(dep_graph)

    item_lookup = _DictUnionView([
            new_temp_vars,
            new_arg_dict
            ])
    type_inf_mapper = TypeInferenceMapper(kernel, callables_table,
            item_lookup)

    from loopy.symbolic import SubstitutionRuleExpander
    subst_expander = SubstitutionRuleExpander(kernel.substitutions)

    # {{{ work on type inference queue

    from loopy.kernel.data import TemporaryVariable, KernelArgument

    old_calls_to_new_calls = {}

    for var_chain in sccs:
        changed_during_last_queue_run = False
        queue = var_chain[:]
        failed_names = set()

        while queue or changed_during_last_queue_run:
            if not queue and changed_during_last_queue_run:
                changed_during_last_queue_run = False
                # Optimization: If there's a single variable in the SCC without
                # a self-referential dependency, then the type is known after a
                # single iteration (we don't need to look at the expressions
                # again).
                if len(var_chain) == 1:
                    single_var, = var_chain
                    if single_var not in dep_graph[single_var]:
                        break
                queue = var_chain[:]

            name = queue.pop(0)
            item = item_lookup[name]

            debug("inferring type for %s %s", type(item).__name__, item.name)
            try:
                (result, symbols_with_unavailable_types,
                        new_old_calls_to_new_calls, callables_table) = (
                        _infer_var_type(
                                kernel, item.name, type_inf_mapper, subst_expander))
            except DependencyTypeInferenceFailure:
                result = tuple()
            type_inf_mapper = type_inf_mapper.copy(
                    callables_table=callables_table)

            failed = not result
            if not failed:
                new_dtype, = result
                if new_dtype.target is None:
                    new_dtype = new_dtype.with_target(kernel.target)

                debug("     success: %s", new_dtype)
                if new_dtype != item.dtype:
                    debug("     changed from: %s", item.dtype)
                    changed_during_last_queue_run = True

                    if isinstance(item, TemporaryVariable):
                        new_temp_vars[name] = item.copy(dtype=new_dtype)
                    elif isinstance(item, KernelArgument):
                        new_arg_dict[name] = item.copy(dtype=new_dtype)
                    else:
                        raise LoopyError("unexpected item type in type inference")
                old_calls_to_new_calls.update(new_old_calls_to_new_calls)
            else:
                debug("     failure")

            if failed:
                if item.name in failed_names:
                    # this item has failed before, give up.
                    advice = ""
                    if symbols_with_unavailable_types:
                        advice += (
                                " (need type of '%s'--check for missing arguments)"
                                % ", ".join(symbols_with_unavailable_types))

                    if expect_completion:
                        raise LoopyError(
                                "could not determine type of '%s'%s"
                                % (item.name, advice))

                    else:
                        # We're done here.
                        break

                # remember that this item failed
                failed_names.add(item.name)

                if set(queue) == failed_names:
                    # We did what we could...
                    print(queue, failed_names, item.name)
                    assert not expect_completion
                    break

                # can't infer type yet, put back into queue
                queue.append(name)
            else:
                # we've made progress, reset failure markers
                failed_names = set()

    # }}}

    # FIXME: copy the explanation from make_function_ready_for_codegen
    # here.

    # {{{ check if insn missed during type inference

    def _instruction_missed_during_inference(insn):
        for assignee in insn.assignees:
            if isinstance(assignee, Lookup):
                assignee = assignee.aggregate

            if isinstance(assignee, Variable):
                if assignee.name in kernel.arg_dict:
                    if kernel.arg_dict[assignee.name].dtype is None:
                        return False
                else:
                    assert assignee.name in kernel.temporary_variables
                    if kernel.temporary_variables[assignee.name].dtype is None:
                        return False

            elif isinstance(assignee, (Subscript, LinearSubscript)):
                if assignee.aggregate.name in kernel.arg_dict:
                    if kernel.arg_dict[assignee.aggregate.name].dtype is None:
                        return False
                else:
                    assert assignee.aggregate.name in kernel.temporary_variables
                    if kernel.temporary_variables[
                            assignee.aggregate.name].dtype is None:
                        return False
            else:
                assert isinstance(assignee, SubArrayRef)
                if assignee.subscript.aggregate.name in kernel.arg_dict:
                    if kernel.arg_dict[
                            assignee.subscript.aggregate.name].dtype is None:
                        return False
                else:
                    assert assignee.subscript.aggregate.name in (
                            kernel.temporary_variables)
                    if kernel.temporary_variables[
                            assignee.subscript.aggregate.name] is None:
                        return False

        return True

    # }}}

    for insn in kernel.instructions:
        if isinstance(insn, lp.MultiAssignmentBase):
            # just a dummy run over the expression, to pass over all the
            # functions
            if _instruction_missed_during_inference(insn):
                type_inf_mapper(insn.expression,
                        return_tuple=len(insn.assignees) > 1,
                        return_dtype_set=True)
        elif isinstance(insn, (_DataObliviousInstruction,
                lp.CInstruction)):
            pass
        else:
            raise NotImplementedError("Unknown instructions type %s." % (
                type(insn).__name__))

    callables_table = type_inf_mapper.callables_table
    old_calls_to_new_calls.update(type_inf_mapper.old_calls_to_new_calls)

    end_time = time.time()
    logger.debug("type inference took {dur:.2f} seconds".format(
            dur=end_time - start_time))

    pre_type_specialized_knl = unexpanded_kernel.copy(
            temporary_variables=new_temp_vars,
            args=[new_arg_dict[arg.name] for arg in kernel.args],
            )

    type_specialized_kernel = change_names_of_pymbolic_calls(
            pre_type_specialized_knl, old_calls_to_new_calls)

    # the check is unnecessary as we would first get TypeInfereceFailure before
    # encountering this. Move this at the start once ManglerCallable is
    # deprecated.
    if expect_completion:
        # if completion is expected, then it is important that all the
        # callables are scoped.
        from loopy.check import check_functions_are_resolved
        check_functions_are_resolved(type_specialized_kernel)

    return type_specialized_kernel, callables_table


def infer_unknown_types(program, expect_completion=False):
    """Infer types on temporaries and arguments."""

    callables_table = program.callables_table

    type_uninferred_knl_callable = (
            callables_table[program.name])
    type_uninferred_root_kernel = type_uninferred_knl_callable.subkernel

    old_callables_count = callables_table.callables_count
    callables_table = (
            program.callables_table.with_edit_callables_mode())
    root_kernel, callables_table = (
            infer_unknown_types_for_a_single_kernel(
                type_uninferred_root_kernel,
                callables_table, expect_completion))

    type_inferred_knl_callable = type_uninferred_knl_callable.copy(
            subkernel=root_kernel)

    callables_table, _ = (
            callables_table.with_callable(
                program.name,
                type_inferred_knl_callable))

    callables_table = (
            callables_table.with_exit_edit_callables_mode(
                old_callables_count))

    return program.copy(callables_table=callables_table)

# }}}


# {{{ reduction expression helper

def infer_arg_and_reduction_dtypes_for_reduction_expression(
        kernel, expr, callables_table, unknown_types_ok):
    type_inf_mapper = TypeInferenceMapper(kernel, callables_table)
    import loopy as lp

    if expr.is_tuple_typed:
        arg_dtypes_result = type_inf_mapper(
                expr, return_tuple=True, return_dtype_set=True)

        if len(arg_dtypes_result) == 1:
            arg_dtypes = arg_dtypes_result[0]
        else:
            if unknown_types_ok:
                arg_dtypes = [lp.auto] * expr.operation.arg_count
            else:
                raise LoopyError("failed to determine types of accumulators for "
                        "reduction '%s'" % expr)
    else:
        try:
            arg_dtypes = [type_inf_mapper(expr)]
        except DependencyTypeInferenceFailure:
            if unknown_types_ok:
                arg_dtypes = [lp.auto]
            else:
                raise LoopyError("failed to determine type of accumulator for "
                        "reduction '%s'" % expr)

    reduction_dtypes = expr.operation.result_dtypes(kernel, *arg_dtypes)
    reduction_dtypes = tuple(
            dt.with_target(kernel.target)
            if dt is not lp.auto else dt
            for dt in reduction_dtypes)

    return tuple(arg_dtypes), reduction_dtypes, (
            type_inf_mapper.callables_table)

# }}}

# vim: foldmethod=marker
