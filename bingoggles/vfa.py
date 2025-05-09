# vfa: Variable Flow Analysis
from binaryninja.variable import Variable
from binaryninja.mediumlevelil import (
    MediumLevelILVar,
    MediumLevelILRet,
    MediumLevelILVarSsa,
)
from binaryninja.highlevelil import HighLevelILOperation
from binaryninja import Function
from binaryninja.enums import SymbolType
from functools import cache
from colorama import Fore

from .bingoggles_types import *
from .auxiliary import *
from binaryninja.enums import MediumLevelILOperation
from binaryninja import BinaryView
from collections import OrderedDict
from typing import List, Union


class Analysis:
    def __init__(
        self,
        binaryview: BinaryView,
        verbose: bool = False,
        libraries_mapped: dict = None,
    ):
        """
        Performs interprocedural variable flow and taint analysis using Binary Ninja's MLIL and HLIL.

        This class powers core features of BinGoggles such as forward/backward slicing, taint propagation,
        interprocedural variable tracking, and detection of tainted function parameters or return values.
        It supports analysis of global variables, function parameters, struct members, and imported functions.

        Attributes:
            bv (BinaryView): The Binary Ninja view object for the binary under analysis.
            verbose (bool): Enables verbose logging for debugging and visibility.
            libraries_mapped (dict): Optional mapping of library BinaryViews for analyzing imported functions.

        Methods:
            get_sliced_calls(data, func_name, propagated_vars) -> dict | None:
                Identifies function calls in the slice where tainted variables are passed as arguments.

            tainted_slice(target, var_type, output, slice_type) -> tuple[list, str, list[Variable]] | None:
                Performs forward or backward taint slicing on the specified variable within its function.

            complete_slice(target, var_type, slice_type, output, analyze_imported_functions) -> dict:
                Traces taint propagation across functions, forming a complete variable flow graph.

            is_function_param_tainted(function_node, tainted_params, ...) -> InterprocTaintResult:
                Recursively determines whether a function's parameters or return value become tainted.
        """
        self.bv = binaryview
        self.verbose = verbose
        self.libraries_mapped = libraries_mapped
        self.glob_refs_memoized = {}

        if self.verbose:
            self.is_function_param_tainted_printed = False

    def get_sliced_calls(
        self,
        data: List[TaintedLOC],
        func_name: str,
        propagated_vars: List[
            Union[TaintedVar, TaintedGlobal, TaintedAddressOfField, TaintedStructMember]
        ],
    ) -> dict | None:
        """
        Identify and extract function calls within a tainted slice, along with their metadata.

        This method scans through a list of tainted locations (TaintedLOC) and collects all
        MLIL function call instructions where any of the arguments match propagated tainted variables.
        For each call, it attempts to resolve the destination function and builds a parameter map
        of tainted arguments.

        Args:
            data (List[TaintedLOC]): The list of locations visited during the taint slice.
            func_name (str): The name of the function being analyzed.
            propagated_vars (List[Union[TaintedVar, TaintedGlobal, TaintedAddressOfField, TaintedStructMember]]):
                The list of tainted variables that were propagated during the slice.

        Returns:
            dict | None: A dictionary mapping `(func_name, call_addr)` tuples to a tuple containing:
                - The resolved call target's name (`str`)
                - The target function's start address (`int`)
                - The MLIL call instruction (`MediumLevelILInstruction`)
                - A dictionary of argument indices to their tainted variable matches (`dict`)
            Returns `None` if no matching calls are found.
        """
        if self.verbose:
            print(
                f"\n{Fore.LIGHTRED_EX}get_sliced_calls{Fore.RESET}({Fore.MAGENTA}self, data: list, func_name: str, verbose: list{Fore.RESET})\n{f'{Fore.GREEN}={Fore.RESET}' * 65}"
            )

        function_calls = {}

        for taintedloc in data:
            addr = taintedloc.addr
            loc = taintedloc.loc

            if int(loc.operation) == int(MediumLevelILOperation.MLIL_CALL):
                param_map = param_var_map(loc.params, propagated_vars)
                call_function_object = addr_to_func(self.bv, int(str(loc.dest), 16))
                if call_function_object:
                    function_addr = call_function_object.start
                    call_name = call_function_object.name

                else:
                    continue

                key = (func_name, addr)

                if self.verbose:
                    print(
                        f"({key[0]}, {key[1]:#0x}): {call_name}, {function_addr:#0x}, {loc}, {param_map}"
                    )

                function_calls[key] = (
                    call_name,
                    function_addr,
                    loc,
                    param_map,
                )

        return function_calls

    @cache
    def tainted_slice(
        self,
        target: TaintTarget,
        var_type: SlicingID,
        output: OutputMode = OutputMode.Returned,
        slice_type: SliceType = SliceType.Forward,
    ) -> tuple[list, str, list[Variable]] | None:
        """
        Perform a taint analysis slice (forward or backward) from a specified target variable.

        This function identifies and traces the propagation of a variable (local, global,
        struct member, or function parameter) through a function using either forward or
        backward slicing. It returns a list of program locations affected by the variable
        and a list of propagated variables.

        Args:
            target (TaintTarget): The target variable to slice from, including its name and location.
            var_type (SlicingID): The classification of the target variable
                (FunctionVar, GlobalVar, StructMember, or FunctionParam).
            output (OutputMode, optional): Whether to print the slice results or return them.
                Defaults to OutputMode.Returned.
            slice_type (SliceType, optional): Direction of the slice (Forward or Backward).
                Defaults to SliceType.Forward.

        Returns:
            tuple[list, str, list[Variable]] | None:
                - A list of `TaintedLOC` objects representing the instructions visited during the slice.
                - The name of the function containing the slice.
                - A list of variables propagated during the slice.
                Returns `None` if the analysis fails due to unresolved instruction or function context.
        """
        if hasattr(target.loc_address, "start"):
            func_obj = target.loc_address

        else:
            func_obj = addr_to_func(self.bv, target.loc_address)
            if func_obj is None:
                print(
                    f"[{Fore.RED}Error{Fore.RESET}] Could not find a function containing address: {target.loc_address}"
                )
                return None

        sliced_func = {}
        propagated_vars = []

        instr_mlil = None
        if var_type != SlicingID.FunctionParam:
            instr_mlil = func_obj.get_llil_at(target.loc_address).mlil

            if instr_mlil is None:
                print(
                    f"[{Fore.RED}Error{Fore.RESET}] Could not find MLIL instruction at address: {target.loc_address}"
                )
                return None

        # Start by tracing the initial target variable
        match var_type:
            # Handle case where the target var for slicing is a function var
            case SlicingID.FunctionVar:
                var_object = str_to_var_object(target.variable, func_obj)

                if var_object:
                    # Handle regular variables
                    if slice_type == SliceType.Forward:
                        sliced_func, propagated_vars = trace_tainted_variable(
                            analysis=self,
                            function_object=func_obj,
                            mlil_loc=instr_mlil,
                            variable=var_object,
                            trace_type=SliceType.Forward,
                        )

                    elif slice_type == SliceType.Backward:
                        sliced_func, propagated_vars = trace_tainted_variable(
                            analysis=self,
                            function_object=func_obj,
                            mlil_loc=instr_mlil,
                            variable=var_object,
                            trace_type=SliceType.Backward,
                        )

                    else:
                        raise TypeError(
                            f"[{Fore.RED}ERROR{Fore.RESET}] slice_type must be either forward or backward"
                        )

            case SlicingID.GlobalVar:
                # Handle Globals
                symbol = [
                    s
                    for s in self.bv.get_symbols()
                    if int(s.type) == int(SymbolType.DataSymbol)
                    and s.name == target.variable
                ]
                if symbol:
                    constr_ptr = None

                    for op in flat(instr_mlil.operands):
                        if hasattr(op, "address"):
                            s = get_symbol_from_const_ptr(self.bv, op)
                            if s and s == symbol:
                                constr_ptr = op
                                break

                    tainted_global = TaintedGlobal(
                        variable=target.variable,
                        confidence_level=TaintConfidence.Tainted,
                        loc_address=target.loc_address,
                        const_ptr=constr_ptr,
                        symbol_object=symbol,
                    )

                    if slice_type == SliceType.Forward:
                        sliced_func, propagated_vars = trace_tainted_variable(
                            analysis=self,
                            function_object=func_obj,
                            mlil_loc=instr_mlil,
                            variable=tainted_global,
                            trace_type=SliceType.Forward,
                        )

                    elif slice_type == SliceType.Backward:
                        sliced_func, propagated_vars = trace_tainted_variable(
                            analysis=self,
                            function_object=func_obj,
                            mlil_loc=instr_mlil,
                            variable=tainted_global,
                            trace_type=SliceType.Backward,
                        )

                    else:
                        raise TypeError(
                            f"[{Fore.RED}ERROR{Fore.RESET}] slice_type must be either forward or backward"
                        )

            case SlicingID.StructMember:
                # Handle struct member references/derefernces
                instr_hlil = func_obj.get_llil_at(target.loc_address).hlil
                struct_offset = instr_mlil.ssa_form.src.offset
                source = instr_mlil.src
                source_hlil = instr_hlil.src
                base_var = source_hlil.var

                if instr_hlil.operation == int(HighLevelILOperation.HLIL_ASSIGN):
                    destination = instr_hlil.dest

                    if destination.operation == int(
                        HighLevelILOperation.HLIL_DEREF_FIELD
                    ):
                        struct_offset = destination.offset
                        base_expr = destination.src

                        if base_expr.operation == int(HighLevelILOperation.HLIL_VAR):
                            base_var = base_expr.var
                            tainted_struct_member = TaintedStructMember(
                                loc_address=target.loc_address,
                                member=target.variable,
                                offset=struct_offset,
                                hlil_var=base_var,
                                variable=instr_mlil.dest.var,
                                confidence_level=TaintConfidence.Tainted,
                            )

                            if slice_type == SliceType.Forward:
                                sliced_func, propagated_vars = trace_tainted_variable(
                                    analysis=self,
                                    function_object=func_obj,
                                    mlil_loc=instr_mlil,
                                    variable=tainted_struct_member,
                                    trace_type=SliceType.Forward,
                                )

                            elif slice_type == SliceType.Backward:
                                sliced_func, propagated_vars = trace_tainted_variable(
                                    analysis=self,
                                    function_object=func_obj,
                                    mlil_loc=instr_mlil,
                                    variable=tainted_struct_member,
                                    trace_type=SliceType.Backward,
                                )

                            else:
                                raise TypeError(
                                    f"[{Fore.RED}ERROR{Fore.RESET}] slice_type must be either forward or backward"
                                )

                elif instr_mlil.operation == int(MediumLevelILOperation.MLIL_SET_VAR):
                    if source.operation == int(MediumLevelILOperation.MLIL_LOAD_STRUCT):
                        tainted_struct_member = TaintedStructMember(
                            loc_address=target.loc_address,
                            member=target.variable,
                            offset=struct_offset,
                            hlil_var=base_var,
                            variable=instr_mlil.src.src.var,
                            confidence_level=TaintConfidence.Tainted,
                        )

                        if slice_type == SliceType.Forward:
                            sliced_func, propagated_vars = trace_tainted_variable(
                                analysis=self,
                                function_object=func_obj,
                                mlil_loc=instr_mlil,
                                variable=tainted_struct_member,
                                trace_type=SliceType.Forward,
                            )

                        elif slice_type == SliceType.Backward:
                            sliced_func, propagated_vars = trace_tainted_variable(
                                analysis=self,
                                function_object=func_obj,
                                mlil_loc=instr_mlil,
                                variable=tainted_struct_member,
                                trace_type=SliceType.Backward,
                            )

                        else:
                            raise TypeError(
                                f"[{Fore.RED}ERROR{Fore.RESET}] slice_type must be either forward or backward"
                            )

                    elif source.operation == int(MediumLevelILOperation.MLIL_VAR_FIELD):
                        tainted_struct_member = TaintedStructMember(
                            loc_address=target.loc_address,
                            member=target.variable,
                            offset=struct_offset,
                            hlil_var=base_var,
                            variable=instr_mlil.src.src,
                            confidence_level=TaintConfidence.Tainted,
                        )

                        if slice_type == SliceType.Forward:
                            sliced_func, propagated_vars = trace_tainted_variable(
                                analysis=self,
                                function_object=func_obj,
                                mlil_loc=instr_mlil,
                                variable=tainted_struct_member,
                                trace_type=SliceType.Forward,
                            )

                        elif slice_type == SliceType.Backward:
                            sliced_func, propagated_vars = trace_tainted_variable(
                                analysis=self,
                                function_object=func_obj,
                                mlil_loc=instr_mlil,
                                variable=tainted_struct_member,
                                trace_type=SliceType.Backward,
                            )

                else:
                    raise ValueError(
                        f"[{Fore.RED}ERORR{Fore.RESET}]Couldn't find variable reference, insure that you're using the MLIL to identify your target variable"
                    )

            # In cases for function params they dont need to be used anywhere where a variable is being assigned for the first time or whatever
            # so we handle it differently than a normal variable, then can simply be passed into lines of code of function calls.
            case SlicingID.FunctionParam:
                if isinstance(target.variable, str):
                    target_param = find_param_by_name(
                        func_obj=func_obj, param_name=target.variable
                    )

                elif isinstance(target.variable, MediumLevelILVar):
                    target_param = target.variable.var

                else:
                    target_param = find_param_by_name(
                        func_obj=func_obj, param_name=target.variable.name
                    )

                try:
                    param_refs = func_obj.get_mlil_var_refs(target_param)

                except AttributeError:
                    raise AttributeError(
                        f"[{Fore.RED}Error{Fore.RESET}] Couldn't find the parameter reference"
                    )

                first_ref_addr = [
                    i.address
                    for i in param_refs
                    if func_obj.get_llil_at(i.address).mlil is not None
                ][0]

                first_ref_mlil = func_obj.get_llil_at(first_ref_addr).mlil
                sliced_func, propagated_vars = trace_tainted_variable(
                    analysis=self,
                    function_object=func_obj,
                    mlil_loc=first_ref_mlil,
                    variable=target_param,
                    trace_type=SliceType.Forward,
                )

            case _:
                raise TypeError(
                    f"{Fore.RED}var_type must be either SlicingID.FunctionParam or SlicingID.FunctionVar, please see the SlicingID class{Fore.RESET}"
                )

        match output:
            case OutputMode.Printed:
                print(
                    f"Address | LOC | Target Variable | Propagated Variable\n{(Fore.LIGHTGREEN_EX+'-'+Fore.RESET)*53}"
                )

                for _, data in sorted(sliced_func.items(), key=lambda item: item[0]):
                    for d in data:
                        print(d.loc.instr_index, d)

            case OutputMode.Returned:
                if self.verbose:
                    print(
                        f"Address | LOC | Target Variable | Propagated Variable | Taint Confidence\n{(Fore.LIGHTGREEN_EX+'-'+Fore.RESET)*72}"
                    )
                    for i in sliced_func:
                        print(i.loc.instr_index, i)

                return (
                    [
                        i for i in sliced_func
                    ],  # list of the collected loc as TaintedLOC objects
                    func_obj.name,  # Target function name
                    propagated_vars,  # List of the propagated variables as TaintedVar objects
                )

            case _:
                raise TypeError(
                    f"[{Fore.RED}ERROR{Fore.RESET}]output_mode must be either OutputMode.Printed or OutputMode.Returned"
                )

    @cache
    def complete_slice(
        self,
        target: TaintTarget,
        var_type: SlicingID,
        slice_type: SliceType = SliceType.Forward,
        output: OutputMode = OutputMode.Returned,
        analyze_imported_functions=False,
    ) -> dict:
        """
        Perform a complete interprocedural taint slice, including cross-function propagation.

        This method begins at a specified taint source (variable or parameter) and traces
        taint propagation through a function and all subsequent function calls where taint
        is passed as a parameter. It builds a call graph-like structure showing how the
        taint flows across functions.

        Args:
            target (TaintTarget): The initial source of taint, defined by an address and variable.
            var_type (SlicingID): The type of variable to slice from (FunctionVar, FunctionParam, etc.).
            slice_type (SliceType, optional): The direction of the slice (Forward or Backward).
                Defaults to SliceType.Forward.
            output (OutputMode, optional): Whether to print results or return them.
                Defaults to OutputMode.Returned.
            analyze_imported_functions (bool, optional): Whether to trace into imported (external) functions.
                Defaults to False.

        Returns:
            dict: An ordered dictionary mapping each function slice as:
                {
                    (function_name, variable): (
                        List[TaintedLOC],            # Slice instructions
                        List[TaintedVar]             # Variables tainted in this slice
                    ),
                    ...
                }

        Raises:
            TypeError: If the starting function cannot be resolved from the provided target address.
        """
        # Initialization of necessary data structures
        propagation_cache = {}  # To store the slice data and propagated variables
        propagated_vars = []  # List of variables that have been tainted and propagated
        calls_analyzed = (
            []
        )  # Keeps track of function calls that have already been analyzed
        call_flow = []  # Maintains the order in which functions are called (call graph)

        # Try to get the initial slice data and parent function information
        try:
            slice_data, og_func_name, propagated_vars = self.tainted_slice(
                target=target,
                var_type=var_type,
                slice_type=slice_type,
            )
        except TypeError:
            raise TypeError(
                f"[{Fore.RED}ERROR{Fore.RESET}] Address is likely wrong in target | got: {target.loc_address:#0x} for {target.variable}"
            )

        # Fetch the parent function object for variable reference
        parent_func_obj = func_name_to_object(self, og_func_name)
        key = (og_func_name, str_to_var_object(target.variable, parent_func_obj))

        # Store the initial slice data into the cache and track the call flow
        propagation_cache[key] = (slice_data, propagated_vars)
        call_flow.append(key)

        # Get any sliced calls from the initial slice data
        sliced_calls = self.get_sliced_calls(slice_data, og_func_name, propagated_vars)

        # If no sliced calls, return the single function slice result
        if sliced_calls is None:
            return OrderedDict((k, propagation_cache[k]) for k in call_flow)

        # Identify imported functions that should not be analyzed further
        imported_functions = {
            i.name
            for i in self.bv.get_symbols_of_type(SymbolType.ImportedFunctionSymbol)
        }

        # Create a dictionary of sliced calls to analyze
        sliced_calls_to_analyze = dict(sliced_calls)

        # Continue analyzing function calls in the call graph until all calls are processed
        while sliced_calls_to_analyze:
            new_calls = {}

            # Iterate over each function call to be analyzed
            for key, data in sliced_calls_to_analyze.items():
                call_name = data[0]
                if analyze_imported_functions and call_name in imported_functions:
                    continue

                loc_addr = key[1]  # Location address of the function call
                func_addr = data[1]  # Address of the function being called
                param_map = data[3]  # Mapping of parameters for the function call
                func_param_to_analyze = (
                    None  # Variable to store the function parameter to analyze
                )

                # Check the parameters of the function call
                for param_name, arg_info in param_map.items():
                    arg_pos = arg_info[
                        1
                    ]  # The position of the argument in the function

                    # Ensure the parameter variable is among the propagated variables
                    if param_name.var not in [v.variable for v in propagated_vars]:
                        continue

                    # If we've already analyzed this function call, skip it
                    analyzed_key = (param_name, arg_pos, call_name, loc_addr)
                    if analyzed_key in calls_analyzed:
                        continue
                    calls_analyzed.append(analyzed_key)

                    # Retrieve the function object and find the relevant parameter
                    func_obj = addr_to_func(self.bv, func_addr)
                    for index, param in enumerate(func_obj.parameter_vars, 1):
                        if index == arg_pos:
                            func_param_to_analyze = param
                            break

                    # Skip if no valid parameter to analyze
                    if not func_param_to_analyze:
                        continue

                    # Slice the function call and get new propagated variables
                    new_slice_data, func_name, propagated_vars = self.tainted_slice(
                        target=TaintTarget(func_addr, func_param_to_analyze),
                        var_type=SlicingID.FunctionParam,
                        slice_type=slice_type,
                    )

                    # Store the new slice data and propagate variables in the cache
                    new_key = (func_name, func_param_to_analyze)
                    if new_key not in propagation_cache:
                        propagation_cache[new_key] = (new_slice_data, propagated_vars)
                        call_flow.append(new_key)

                    # Get any new sliced calls from this function and add them to the analysis queue
                    new_sliced_calls = self.get_sliced_calls(
                        new_slice_data, func_name, propagated_vars
                    )
                    if new_sliced_calls:
                        new_calls.update(new_sliced_calls)

            # Update the list of sliced calls to analyze for the next iteration
            sliced_calls_to_analyze = new_calls

        # Output mode handling: return or print the results
        if output == OutputMode.Printed:
            for k in call_flow:
                fn_name, var = k
                print(
                    f"Function: {fn_name} | Var: {var.name if hasattr(var, 'name') else var}"
                )
                for entry in propagation_cache[k][0]:
                    print(entry)

        # Return the final ordered dictionary of function slices
        return OrderedDict((k, propagation_cache[k]) for k in call_flow)

    def is_function_param_tainted(
        self,
        function_node: int | Function,
        tainted_params: Variable | str | list[Variable],
        origin_function: Function = None,
        original_tainted_params: Variable | str | list[Variable] = None,
        tainted_param_map: dict = None,
        recursion_limit=8,
        sub_functions_analyzed=0,
    ):
        """
        Perform interprocedural taint analysis to determine if a function's parameters or return value are tainted.

        This method takes an entry function and a set of tainted parameters, then performs a taint analysis
        on the function body and recursively on any called sub-functions to determine whether any of its parameters
        or return value are affected. It tracks assignments, calls, field operations, and propagates taint accordingly.

        Args:
            function_node (int | Function): Either the starting address or Binary Ninja `Function` object to analyze.
            tainted_params (Variable | str | list[Variable]): The parameter(s) to start tracking taint from.
                Can be a single Variable, a parameter name (str), or a list of Variable objects.
            origin_function (Function, optional): Internally used to identify the root function when tracing across calls.
            original_tainted_params (Variable | str | list[Variable], optional): Original tainted input for context.
            tainted_param_map (dict, optional): A mapping from original parameters to other discovered tainted parameters.
            recursion_limit (int, optional): The maximum recursion depth for interprocedural analysis. Defaults to 8.
            sub_functions_analyzed (int, optional): Internal counter for recursion depth tracking.

        Returns:
            InterprocTaintResult: A structured result containing:
                - `tainted_param_names` (set[str]): Parameter names determined to be tainted.
                - `original_tainted_variables`: The original tainted parameter(s) used as input.
                - `is_return_tainted` (bool): Whether the return value of the function is tainted.
                - `tainted_param_map` (dict): A mapping of original parameter to other tainted parameters.

        Raises:
            ValueError: If the given address does not resolve to a function.
        """
        if tainted_param_map is None:
            tainted_param_map = {}

        def walk_variable(var_mapping: dict, key_names: set):
            """
            Recursively traverse the variable mapping to find all variables influenced by the tainted variables.

            Args:
                var_mapping (dict): A dictionary mapping variables to the set of variables they influence.
                key_names (set): A set of variable names to start the traversal from.

            Returns:
                set: A set of all variable names influenced by the initial key_names.
            """
            if not any(var in var_mapping for var in key_names):
                return set(key_names)

            new_variables = set()

            for var_name in key_names:
                if var_name in var_mapping:
                    # print("adding var to new variables: ", var_name)
                    new_variables.update(var_mapping[var_name])

                else:
                    new_variables.add(var_name)

            return walk_variable(var_mapping, new_variables)

        # Convert function_node to a Function object if it's provided as an integer address
        if isinstance(function_node, int):
            addr = function_node
            function_node = addr_to_func(self.bv, function_node)
            if function_node is None:
                raise ValueError(
                    f"[{Fore.RED}ERROR{Fore.RESET}]Could not find target function from address @ {addr:#0x}"
                )

        if origin_function is None:
            origin_function = function_node

        if original_tainted_params is None:
            original_tainted_params = tainted_params

        # Initialize a set for TaintedVar objects
        tainted_variables = set()

        def get_ssa_variable(func, var: Variable):
            if isinstance(var, SSAVariable):
                return var

            for ssa_var in func.mlil.ssa_form.vars:
                if ssa_var.var == var:
                    return ssa_var

        # Ensure tainted_params is a list for consistent processing
        if not isinstance(tainted_params, list):
            tainted_params = [get_ssa_variable(function_node, tainted_params)]

        if self.verbose and not self.is_function_param_tainted_printed:
            print(
                # f"\n{Fore.LIGHTRED_EX}is_function_param_tainted{Fore.RESET}({Fore.MAGENTA}self, function_node: int | Function, "
                f"tainted_params: Variable | str | list[Variable]{Fore.RESET})\n-> {Fore.LIGHTBLUE_EX}{function_node}"
                f"{Fore.RESET}:{Fore.BLUE}{tainted_params}{Fore.RESET}\n{Fore.GREEN}{'='*113}{Fore.RESET}"
            )
            self.is_function_param_tainted_printed = True

        # Convert string parameter names to Variable objects in SSA form and wrap them in TaintedVar
        for param in tainted_params:
            if isinstance(param, str):
                try:
                    var_obj = str_param_to_var_object(
                        function_node, param, ssa_form=True
                    )
                    tainted_variables.add(
                        TaintedVar(
                            var_obj,
                            TaintConfidence.Tainted,
                            function_node.start,
                        )
                    )

                except ValueError:
                    continue

            elif isinstance(param, Variable):
                tainted_variables.add(
                    TaintedVar(
                        param,
                        TaintConfidence.Tainted,
                        function_node.start,
                    )
                )

        variable_mapping = {}
        tainted_parameters = set()

        # Iterate through each MLIL block in the function
        for mlil_block in function_node.mlil:
            for instr in mlil_block:
                loc = instr.ssa_form

                match int(loc.operation):
                    # Handle SSA store operation by wrapping the destination variable in TaintedVar
                    case int(MediumLevelILOperation.MLIL_STORE_SSA):
                        address_variable, offset_variable = None, None
                        offset_var_taintedvar = None
                        addr_var = None
                        offset = None

                        if len(loc.dest.operands) == 1:
                            address_variable = loc.dest.operands[0]

                        elif len(loc.dest.operands) == 2:
                            address_variable, offset_variable = loc.dest.operands
                            addr_var, offset = address_variable.operands

                        if offset_variable:
                            offset_var_taintedvar = [
                                var.variable
                                for var in variable_mapping
                                if var.variable == offset_variable
                            ]

                        if offset_var_taintedvar:
                            tainted_variables.add(
                                TaintedAddressOfField(
                                    variable=addr_var or address_variable,
                                    offset=offset,
                                    offset_var=offset_var_taintedvar,
                                    confidence_level=TaintConfidence.Tainted,
                                    loc_address=loc.address,
                                    targ_function=function_node,
                                )
                            )

                        else:
                            tainted_variables.add(
                                TaintedAddressOfField(
                                    variable=addr_var or address_variable,
                                    offset=offset,
                                    offset_var=TaintedVar(
                                        variable=offset_variable,
                                        confidence_level=TaintConfidence.NotTainted,
                                        loc_address=loc.address,
                                    ),
                                    confidence_level=TaintConfidence.Tainted,
                                    loc_address=loc.address,
                                    targ_function=function_node,
                                )
                            )

                    # Handle SSA load operation by wrapping the source variable in TaintedVar
                    case int(MediumLevelILOperation.MLIL_SET_VAR_SSA):
                        for var in loc.vars_written:
                            tainted_variables.add(
                                TaintedVar(
                                    var,
                                    TaintConfidence.Tainted,
                                    loc.address,
                                )
                            )

                    # Check if the instruction is a function call in SSA form
                    case int(MediumLevelILOperation.MLIL_CALL_SSA):
                        # Extract the parameters involved in the call
                        call_params = [
                            param
                            for param in loc.params
                            if isinstance(param, MediumLevelILVarSsa)
                        ]

                        call_object = addr_to_func(self.bv, int(str(loc.dest), 16))

                        if self.verbose:
                            print(
                                f"[{Fore.GREEN}INFO{Fore.RESET}] Analyzing sub-function call: {call_object} for tainted parameters"
                            )

                        if not call_object:
                            continue

                        function_call_params = call_object.parameter_vars
                        zipped_params = list(zip(call_params, function_call_params))

                        # Identify sub-function parameters that are tainted by comparing the underlying variable
                        tainted_sub_params = [
                            param[1].name
                            for param in zipped_params
                            if any(
                                tv.variable == param[0].ssa_form.var
                                for tv in tainted_variables
                            )
                        ]

                        if recursion_limit < sub_functions_analyzed:
                            interproc_results = self.is_function_param_tainted(
                                call_object,
                                tainted_sub_params,
                                origin_function,
                                original_tainted_params,
                                tainted_param_map,
                            )

                            sub_functions_analyzed += 1

                            # Map back the tainted sub-function parameters to the current function's variables
                            tainted_sub_variables = [
                                param[0].ssa_form.var
                                for param in zipped_params
                                if param[1].name
                                in interproc_results.tainted_param_names
                            ]

                            for sub_var in tainted_sub_variables:
                                tainted_variables.add(
                                    TaintedVar(
                                        sub_var,
                                        TaintConfidence.Tainted,
                                        loc.address,
                                    )
                                )

                            for ret_var in loc.vars_written:
                                if interproc_results.is_return_tainted:
                                    tainted_variables.add(
                                        TaintedVar(
                                            ret_var,
                                            TaintConfidence.Tainted,
                                            loc.address,
                                        )
                                    )

                    case int(MediumLevelILOperation.MLIL_SET_VAR_SSA_FIELD):
                        tainted_variables.add(
                            TaintedVar(loc.dest, TaintConfidence.Tainted, loc.address)
                        )

                    case _:
                        if loc.operation not in [
                            int(MediumLevelILOperation.MLIL_RET),
                            int(MediumLevelILOperation.MLIL_GOTO),
                            int(MediumLevelILOperation.MLIL_IF),
                        ]:
                            print(
                                "[is_function_param_tainted (WIP)] Unaccounted for operation",
                                loc.operation.name,
                                hex(loc.address),
                                loc,
                            )

                # Map variables written to the variables read in the current instruction
                for var_assignment in loc.vars_written:
                    variable_mapping[var_assignment] = loc.vars_read

                # If any read variable is tainted, mark the written variables as tainted
                if any(
                    any(tv.variable == read_var for tv in tainted_variables)
                    for read_var in loc.vars_read
                ):
                    for written_var in loc.vars_written:
                        tainted_variables.add(
                            TaintedVar(
                                written_var,
                                TaintConfidence.Tainted,
                                loc.address,
                            )
                        )

        # Extract underlying variables from TaintedVar before walking the mapping.
        underlying_tainted = {tv.variable for tv in tainted_variables}

        # Determine all parameters that are tainted by walking through the variable mapping.
        tainted_parameters.update(
            var
            for var in walk_variable(variable_mapping, underlying_tainted)
            if var.name in [param.name for param in origin_function.parameter_vars]
        )

        if len(tainted_parameters) > 1:
            tainted_param_map[list(tainted_parameters)[0]] = list(
                set(tainted_parameters)
            )[1:]

        # Find out if return variable is tainted
        ret_variable_tainted = False

        for t_var in tainted_variables:
            loc = origin_function.get_llil_at(t_var.loc_address)
            """
                Checking variable use sites for each variable in the tainted variables set, 
                if any of them are the return variable, the return variable is tainted.
            """
            if loc:
                var_use_sites = t_var.variable.use_sites

                for use_site in var_use_sites:
                    if isinstance(use_site, MediumLevelILRet):
                        ret_variable_tainted = True

        # DEBUG
        # from pprint import pprint
        # pprint(variable_mapping)
        # pprint(tainted_variables)

        return InterprocTaintResult(
            tainted_param_names=tainted_parameters,
            original_tainted_variables=original_tainted_params,
            is_return_tainted=ret_variable_tainted,
            tainted_param_map=tainted_param_map,
        )

    def is_function_imported(self, instr_mlil: MediumLevelILInstruction) -> bool:
        if instr_mlil.operation != MediumLevelILOperation.MLIL_CALL:
            return False

        call_dest = instr_mlil.dest
        if call_dest.operation == MediumLevelILOperation.MLIL_CONST_PTR:
            func_address = call_dest.constant
        else:
            return False

        target_symbol = self.bv.get_symbol_at(func_address)
        if not target_symbol:
            return False

        if target_symbol.type == SymbolType.ImportedFunctionSymbol.value:
            return target_symbol

        else:
            return False

    #:TODO implement this
    def analyze_imported_function(self, func_symbol):
        for _, lib_binary_view in self.libraries_mapped.items():
            for function in lib_binary_view.functions:
                if function.name == func_symbol.name:
                    print("AAAA", function.mlil)
