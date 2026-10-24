"""Section 7: the memory model as a program sees it (phase 2)."""
from helpers import error_of, run


def test_collections_are_values():
    assert run("a = [1]\nb = a\nb.add(2)\nprint(a, b)\n") == "[1] [1, 2]\n"
    assert run('m = {"x": 1}\nn = m\nn["y"] = 2\nprint(m, n)\n') == '{"x": 1} {"x": 1, "y": 2}\n'
    assert run("s = set(1)\nt = s\nt.add(2)\nprint(s, t)\n") == "set(1) set(1, 2)\n"


def test_a_parameter_is_a_copy_when_changed():
    src = "fn grow(xs):\n    xs.add(9)\n    return xs\na = [1]\nb = grow(a)\nprint(a, b)\n"
    assert run(src) == "[1] [1, 9]\n"


def test_changes_through_fields_and_elements():
    src = (
        "type Box:\n    items: list[int] = []\n"
        "b = Box()\nb.items.add(1)\nalias = b.items\nalias.add(2)\nprint(b.items, alias)\n"
        "grid = [[0, 0], [0, 0]]\nrow = grid[0]\ngrid[0][1] = 5\nprint(grid, row)\n"
        'm = {"k": [1]}\nm["k"].add(2)\nprint(m)\n'
    )
    assert run(src) == '[1] [1, 2]\n[[0, 5], [0, 0]] [0, 0]\n{"k": [1, 2]}\n'


def test_records_are_shared():
    src = "type P:\n    x: int\np = P(1)\nq = p\nq.x = 2\nprint(p.x)\n"
    assert run(src) == "2\n"


def test_a_loop_sees_the_value_it_started_with():
    src = "var xs = [1, 2]\nfor x in xs:\n    xs.add(x * 10)\nprint(xs)\n"
    assert run(src) == "[1, 2, 10, 20]\n"


def test_a_list_added_to_itself_is_a_copy():
    assert run("var xs = [1]\nxs.add(xs)\nprint(xs)\n") == "[1, [1]]\n"


NODE = "type Node:\n    value: int\n    children: list[Node] = []\n    parent: weak Node?\n"


def test_a_weak_field_reads_its_target_while_it_lives():
    src = NODE + "root = Node(1)\nc = Node(2, parent: root)\nroot.children.add(c)\nprint(c.parent.value)\nprint(root)\n"
    assert run(src) == "1\nNode(value: 1, children: [Node(value: 2, children: [], parent: Node(...))], parent: none)\n"


def test_a_weak_field_does_not_keep_its_target_alive():
    src = NODE + "fn make():\n    p = Node(1)\n    return Node(2, parent: p)\nprint(make().parent)\n"
    assert run(src) == "none\n"


def test_weak_rules_are_compile_errors():
    assert error_of("type N:\n    p: weak N\n").key == "weak_optional"
    assert error_of("type N:\n    p: weak int?\n").key == "weak_target"
    assert error_of("x: weak int = 1\n").key == "weak_place"
    assert error_of("type N:\n    p: list[weak N]\n").key == "weak_place"


def test_types_that_refer_to_each_other_need_a_weak_field():
    e = error_of("type A:\n    b: B?\ntype B:\n    a: A?\n")
    assert e.key == "type_cycle" and e.args_["field"] == "a" and e.args_["type"] == "A"
    assert run("type A:\n    b: B?\ntype B:\n    a: weak A?\nprint(1)\n") == "1\n"
    # a type that refers to itself (a list, a tree) is not a cycle
    assert run("type L:\n    next: L?\n    kids: list[L] = []\nprint(L(L(none)).next.next)\n") == "none\n"
