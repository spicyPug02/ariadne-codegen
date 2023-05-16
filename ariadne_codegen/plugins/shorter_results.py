"""
Shorter results by returning single fields directly.

Given a GraphQL schema that looks like this:

    type Query{
        me: User
    }

    type User {
        id: ID!
        username: String!
    }

The codegen will generate a class that has a single attribute `me`.

    class GetAuthenticatedUser(BaseModel):
        me: Optional["GetAuthenticatedUserMe"]

When later using the client to call `get_authenticated_user` the return type
would most of the time immediately be expanded to get the inner
`GetAuthenticatedUserMe`:

    me = (await client.get_authenticated_user).me

By enabling this plugin the return type of the generated
`get_authenticated_user` would instead directly return a
`GetAuthenticatedUserMe` so the caller could use:

    me = await client.get_authenticated_user

This plugin can be enabled by either adding the plugin in the settings:

    plugins = ["ariadne_codegen.ShorterResultsPlugin"]
"""

import ast
from typing import Union, Dict, Optional, List
from graphql import GraphQLSchema, ExecutableDefinitionNode, SelectionSetNode
from ariadne_codegen.codegen import (
    generate_async_for,
    generate_attribute,
    generate_expr,
    generate_name,
    generate_return,
    generate_subscript,
    generate_yield,
)

from ariadne_codegen.plugins.base import Plugin


class ShorterResultsPlugin(Plugin):
    """
    Make single field return types expanded.

    All client method that returns type with a single field will be expanded and
    instead return the type of the inner field.
    """

    def __init__(self, schema: GraphQLSchema, config_dict: Dict) -> None:
        self.class_dict: Dict[str, ast.ClassDef] = {}
        self.extended_imports: Dict[str, set] = {}

        super().__init__(schema, config_dict)

    def generate_result_class(
        self,
        class_def: ast.ClassDef,
        operation_definition: ExecutableDefinitionNode,
        selection_set: SelectionSetNode,
    ) -> ast.ClassDef:
        """Store a map of all classes and its ast"""
        self.class_dict[class_def.name] = class_def

        return super().generate_result_class(
            class_def, operation_definition, selection_set
        )

    def generate_client_module(self, module: ast.Module) -> ast.Module:
        """Add extra import needed after expanding the inner field"""
        if len(self.extended_imports) == 0:
            return super().generate_client_module(module)

        for stmt in module.body:
            # We know we've already imported the wrapping type so we're only
            # interested in `ImportFrom`.
            if not isinstance(stmt, ast.ImportFrom):
                continue

            # The module we import from is always the same as the method we
            # modified when changing the return type.
            if stmt.module not in self.extended_imports:
                continue

            # Add all additional imports discovered when generating the client
            # method.
            for additional_import in self.extended_imports[stmt.module]:
                stmt.names.append(ast.alias(name=additional_import))

        return super().generate_client_module(module)

    def generate_client_method(
        self, method_def: Union[ast.FunctionDef, ast.AsyncFunctionDef]
    ) -> Union[ast.FunctionDef, ast.AsyncFunctionDef]:
        """
        Change the method generated in the client to call the inner field and
        change the return type.
        """
        if len(method_def.body) < 1:
            return super().generate_client_method(method_def)

        last_stmt = method_def.body[-1]

        if isinstance(last_stmt, ast.Return):
            return self._generate_query_and_mutation_client_method(
                method_def, last_stmt
            )
        elif isinstance(last_stmt, ast.AsyncFor):
            return self._generate_subscription_client_method(method_def, last_stmt)

        return super().generate_client_method(method_def)

    def _generate_subscription_client_method(
        self,
        method_def: Union[ast.FunctionDef, ast.AsyncFunctionDef],
        async_for_stmt: ast.AsyncFor,
    ):
        """
        Generate method for subscriptions.
        """
        if not isinstance(method_def.returns, ast.Subscript):
            return super().generate_client_method(method_def)

        if not isinstance(method_def.returns.slice, ast.Name):
            return super().generate_client_method(method_def)

        node_and_class = _return_or_yield_node_and_class(
            method_def.returns.slice.id, self.class_dict
        )
        if node_and_class is None:
            return super().generate_client_method(method_def)

        yield_node, single_field_classes, single_field_return_class = node_and_class

        previous_yield_value = _get_yield_value_from_async_for(method_def.body[-1])
        if previous_yield_value is None:
            return super().generate_client_method(method_def)

        # Update the return type to be the inner field and ensure we reference
        # the single field before returning.
        method_def.returns = generate_subscript(
            value=generate_name("AsyncIterator"),
            slice_=yield_node,
        )

        method_def.body[-1] = generate_async_for(
            target=async_for_stmt.target,
            iter_=async_for_stmt.iter,
            body=generate_expr(
                value=generate_yield(
                    value=generate_attribute(
                        value=previous_yield_value,
                        attr=single_field_return_class,
                    )
                )
            ),
        )

        self._update_imports(method_def, single_field_classes)

        return super().generate_client_method(method_def)

    def _generate_query_and_mutation_client_method(
        self,
        method_def: Union[ast.FunctionDef, ast.AsyncFunctionDef],
        return_stmt: ast.Return,
    ):
        """
        Generate method for query or mutations.
        """
        if return_stmt.value is None:
            return super().generate_client_method(method_def)

        if not isinstance(method_def.returns, ast.Name):
            return super().generate_client_method(method_def)

        node_and_class = _return_or_yield_node_and_class(
            method_def.returns.id, self.class_dict
        )
        if node_and_class is None:
            return super().generate_client_method(method_def)

        return_node, single_field_classes, single_field_return_class = node_and_class

        # Update the return type to be the inner field and ensure we reference
        # the single field before returning.
        method_def.returns = return_node
        method_def.body[-1] = generate_return(
            value=generate_attribute(
                value=return_stmt.value, attr=single_field_return_class
            )
        )

        self._update_imports(method_def, single_field_classes)

        return super().generate_client_method(method_def)

    def _update_imports(
        self,
        method_def: Union[ast.FunctionDef, ast.AsyncFunctionDef],
        single_field_classes: List[str],
    ):
        """
        After expanding to the inner type we will end up with one or more
        classes that we're now returning. Ensure they're all imported in the
        file defining the method.
        """
        for single_field_class in single_field_classes:
            if single_field_class not in self.class_dict:
                return

            # After we change the type we also need to import it in the client if
            # it's one of our generated types so add the extra import as needed.
            if method_def.name not in self.extended_imports:
                self.extended_imports[method_def.name] = set()

            self.extended_imports[method_def.name].add(single_field_class)


def _get_yield_value_from_async_for(stmt: ast.stmt) -> Optional[ast.expr]:
    """
    Extract inner yield value from `ast.AsyncFor` and return `None` if the stmt
    isn't of expected type.
    """
    if not isinstance(stmt, ast.AsyncFor):
        return None

    if len(stmt.body) < 1:
        return None

    if not isinstance(stmt.body[0], ast.Expr):
        return None

    if not isinstance(stmt.body[0].value, ast.Yield):
        return None

    yield_value = stmt.body[0].value.value
    if yield_value is None:
        return None

    return yield_value


def _return_or_yield_node_and_class(
    current_return_class: str, class_dict: Dict[str, ast.ClassDef]
) -> Optional[tuple[ast.expr, List[str], str]]:
    """
    Given there's only a single field in the return type, return the ast for
    that node, what class(es) that is and the name of the field.
    """
    if current_return_class not in class_dict:
        return None

    fields = _get_all_fields(class_dict[current_return_class], class_dict)
    if len(fields) != 1:
        return None

    single_field = fields[0]
    if not isinstance(single_field, ast.AnnAssign):
        return None

    if not isinstance(single_field.target, ast.Name):
        return None

    # Traverse the type annotation until we find the inner type. This can
    # require several iterations if the return type is something like
    # Optional[List[Any]]
    return_node, return_class = _update_node(single_field.annotation)

    return return_node, return_class, single_field.target.id


def _update_node(node: ast.expr) -> tuple[ast.expr, List[str]]:
    """
    Recurse down a node to finner the inner ast.Name. Once found, evaluate the
    potential literal so it gets unquoted and return the inner name.
    """
    if isinstance(node, ast.Name):
        try:
            node.id = ast.literal_eval(node.id)
        except ValueError:
            # Not a literal
            pass

        return node, [node.id]

    if isinstance(node, ast.Subscript):
        node.slice, node_ids = _update_node(node.slice)

        return node, node_ids

    if isinstance(node, ast.Tuple):
        all_node_ids = []
        for i, elt in enumerate(node.elts):
            node.elts[i], node_ids = _update_node(elt)
            all_node_ids.extend(node_ids)

        return node, all_node_ids

    return ast.expr(), []


def _get_all_fields(
    class_def: ast.ClassDef, class_dict: Dict[str, ast.ClassDef]
) -> List[ast.AnnAssign]:
    """
    Recursively get all fields from all inherited classes to figure out the
    total amount of fields.
    """
    fields = []

    for base in class_def.bases:
        if not isinstance(base, ast.Name):
            continue

        if base.id not in class_dict:
            continue

        fields.extend(_get_all_fields(class_dict[base.id], class_dict))

    for field in class_def.body:
        if isinstance(field, ast.AnnAssign):
            fields.append(field)

    return fields
