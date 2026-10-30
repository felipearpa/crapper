from crapper.languages.fsharp import functions_in_source


def entries(source, path="src/Board.fs"):
    return [(fn.namespace, fn.name) for fn in functions_in_source(source, path, "/proj")]


def complexity_of(body, name="choose"):
    source = f"module Demo\n\n{body}"
    found = {fn.name: fn.complexity for fn in functions_in_source(source, "Demo.fs", "/proj")}
    return found[name]


def test_namespace_modules_and_type_members_are_entries():
    source = """
namespace Demo.Core

module Board =
    let place x = x + 1

    module Inner =
        let tick y = y

type Widget(size: int) =
    let half x = x / 2
    member this.Grow(n) = n + size
    static member Make() = Widget(1)
    member _.Area = size * size
    member val Size = size with get, set
    abstract member Paint: unit -> unit
    interface System.IDisposable with
        member _.Dispose() = ()
"""
    assert entries(source) == [
        ("Demo.Core.Board", "place"),
        ("Demo.Core.Board.Inner", "tick"),
        ("Demo.Core.Widget", "half"),
        ("Demo.Core.Widget", "Grow"),
        ("Demo.Core.Widget", "Make"),
        ("Demo.Core.Widget", "Area"),
        ("Demo.Core.Widget", "Dispose"),
    ]


def test_a_top_level_module_line_names_the_namespace():
    source = """
module Demo.Http.Routes

let rec even n = if n = 0 then true else odd (n - 1)
and odd n = if n = 0 then false else even (n - 1)

type Point =
    { X: int }
    static member Zero = { X = 0 }
"""
    assert entries(source) == [
        ("Demo.Http.Routes", "even"),
        ("Demo.Http.Routes", "odd"),
        ("Demo.Http.Routes.Point", "Zero"),
    ]


def test_a_file_without_a_module_line_is_the_module_named_after_the_file():
    source = """
open System

let greet name = "hi " + name
printfn "%s" (greet "x")
"""
    assert entries(source, "src/board.fs") == [("Board", "greet")]
    assert entries(source, "scripts/build.fsx") == [("Build", "greet")]


def test_a_function_with_a_return_type_is_an_entry():
    source = """
namespace Demo

module Text =
    let noneIfEmpty (value: string) : string option =
        if value = "" then None else Some value

    let create () : int = 1
    let private twice x : int = x * 2
"""
    found = functions_in_source(source, "src/Text.fs", "/proj")
    assert [(fn.namespace, fn.name, fn.complexity) for fn in found] == [
        ("Demo.Text", "noneIfEmpty", 2),
        ("Demo.Text", "create", 1),
        ("Demo.Text", "twice", 1),
    ]


def test_a_destructured_union_value_is_not_an_entry():
    source = """
namespace Demo

module Values =
    let Some y = opt
    let Ok (a, b) = r
    let Shape.Circle radius = shape
    let [<Literal>] Some z = opt
    let place x = x + 1
"""
    assert entries(source) == [("Demo.Values", "place")]
    assert entries("module Demo\n\nlet Some y = opt\nlet Ok (a, b) = r\n") == []


def test_a_pascal_case_let_with_arguments_is_read_as_a_union_case_pattern():
    source = """
module Demo.Rules

let Validate x = x > 0
let validate x = x > 0
let Handler = fun x -> x + 1
"""
    assert entries(source) == [("Demo.Rules", "validate"), ("Demo.Rules", "Handler")]


def test_an_active_pattern_is_named_by_its_pattern_text():
    source = """
module Demo.Numbers

let (|Even|Odd|) n = if n % 2 = 0 then Even else Odd
let inline (|Positive|_|) x = if x > 0 then Some x else None
"""
    assert entries(source) == [
        ("Demo.Numbers", "|Even|Odd|"),
        ("Demo.Numbers", "|Positive|_|"),
    ]


def test_a_lambda_bound_to_a_value_is_an_entry():
    source = """
module Demo.Handlers

let handler = fun next ctx -> next ctx
let typed : int -> int = fun x -> x
let wrapped = (fun x -> x + 1)
let pick = function
    | 1 -> "one"
    | _ -> "many"
"""
    assert entries(source) == [
        ("Demo.Handlers", "handler"),
        ("Demo.Handlers", "typed"),
        ("Demo.Handlers", "wrapped"),
        ("Demo.Handlers", "pick"),
    ]


def test_nested_lambdas_partial_application_composition_and_statements_are_not_entries():
    source = """
module Demo.Values

let limit = 10
let partial = List.map id
let composed = partial >> List.rev
let outer x =
    let inner y = y + 1
    let lam = fun z -> if z then 1 else 0
    inner x + lam true
do printfn "ready"
"""
    assert entries(source) == [("Demo.Values", "outer")]


def test_a_nested_lambda_counts_in_the_enclosing_function():
    body = """
let choose xs =
    xs |> List.map (fun x -> if x > 0 then x else 0)
"""
    assert complexity_of(body) == 2


def test_if_elif_for_and_while_each_add_one():
    body = """
let choose x =
    for i in 1 .. x do ()
    for i = 0 to x do ()
    while x > 0 do ()
    if x > 0 then 1
    elif x = 0 then 0
    else -1
"""
    assert complexity_of(body) == 6


def test_each_match_function_and_try_with_arm_adds_one():
    body = """
let choose x =
    let f = function
        | 1 -> 1
        | _ -> 2
    try
        match x with
        | 1 -> f 1
        | 2 -> 2
        | _ -> 3
    with
    | :? System.ArgumentException -> 0
"""
    assert complexity_of(body) == 7


def test_a_when_guard_adds_one_on_top_of_its_arm():
    body = """
let choose x =
    match x with
    | n when n > 2 -> 2
    | _ -> 0
"""
    assert complexity_of(body) == 4


def test_each_and_and_or_adds_one():
    body = """
let choose a b c =
    a && b || c
"""
    assert complexity_of(body) == 3


def test_an_if_condition_is_not_a_guard():
    body = """
let choose ready =
    if ready then 1 else 0
"""
    assert complexity_of(body) == 2


def test_combinators_and_let_bang_add_nothing():
    body = """
let choose input =
    task {
        let! value = input |> Option.map (fun x -> x + 1) |> Task.FromResult
        return value
    }
"""
    assert complexity_of(body) == 1


def test_an_entry_spans_its_definition_lines():
    source = """module Demo

let rec even n =
    n = 0
and odd n =
    n = 1
"""
    found = functions_in_source(source, "Demo.fs", "/proj")
    assert [(fn.name, fn.start_line, fn.end_line, fn.language) for fn in found] == [
        ("even", 3, 4, "fsharp"),
        ("odd", 5, 6, "fsharp"),
    ]


def test_each_arm_of_a_single_line_match_or_try_with_adds_one():
    assert complexity_of("let choose x = match x with | A -> 1 | B -> 2\n") == 3
    assert complexity_of("let choose x = match x with A -> 1 | B -> 2 | C -> 3\n") == 4
    assert complexity_of("let choose x = try 1 with | :? E -> 2 | _ -> 3\n") == 3
    assert complexity_of("let choose x = try 1 with :? E -> 2 | _ -> 3\n") == 3


def test_a_when_guard_on_a_single_line_arm_adds_one():
    assert complexity_of("let choose x = match x with A -> 1 | n when n > 0 -> 2 | _ -> 3\n") == 5


def test_a_function_with_an_attribute_on_its_name_is_an_entry():
    source = """
module Demo.Program

let [<Literal>] limit = 100
let [<EntryPoint>] main argv = 0
let rec a x = x
and [<Foo>] b y = y
"""
    assert entries(source) == [
        ("Demo.Program", "main"),
        ("Demo.Program", "a"),
        ("Demo.Program", "b"),
    ]


def test_a_value_bound_to_an_object_expression_is_an_entry_with_its_members_complexity():
    source = """
module Demo.Resources

let disposable =
    { new System.IDisposable with
        member _.Dispose() = if ready then () else () }

type Holder() =
    let comparer =
        { new System.Collections.Generic.IComparer<int> with
            member _.Compare(a, b) = if a > b then 1 elif a < b then -1 else 0 }
    member _.Comparer = comparer
"""
    found = functions_in_source(source, "src/Resources.fs", "/proj")
    assert [(fn.namespace, fn.name, fn.complexity) for fn in found] == [
        ("Demo.Resources", "disposable", 2),
        ("Demo.Resources.Holder", "comparer", 3),
        ("Demo.Resources.Holder", "Comparer", 1),
    ]
