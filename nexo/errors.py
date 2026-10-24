"""Errors, the section 14 renderer and the message catalog (es, en).

Every message is two sentences: what went wrong and how to fix it. They are
rendered as

    Error in app.nexo, line 4:
      print(nmae)
            ^^^^
    "nmae" does not exist. Did you mean "name"?

Spanish is the default language; `set_lang("en")` switches to English.
"""
from __future__ import annotations

from dataclasses import dataclass

LANG = "es"

# file name -> source text, filled by the lexer so errors can show snippets
SOURCES: dict[str, str] = {}


def set_lang(lang: str) -> None:
    global LANG
    LANG = lang if lang in ("es", "en") else "es"


@dataclass(frozen=True)
class Span:
    file: str
    line: int
    col: int  # 1-based
    end_col: int  # exclusive; same line as `line`

    def to(self, other: "Span") -> "Span":
        if other.line != self.line:
            return Span(self.file, self.line, self.col, 10**9)
        return Span(self.file, self.line, self.col, max(self.end_col, other.end_col))


# --------------------------------------------------------------------------
# Catalog. Each entry: key -> {"es": text, "en": text}. Keys starting with
# "fix." are the second sentence. Placeholders use str.format.

MESSAGES: dict[str, dict[str, str]] = {
    # headers and shared words
    "header": {"es": "Error en {file}, línea {line}:", "en": "Error in {file}, line {line}:"},
    "fix.suggest": {"es": '¿Quisiste decir "{suggestion}"?', "en": 'Did you mean "{suggestion}"?'},
    # lexer
    "indent_tab": {"es": "Hay un tabulador en la indentación.", "en": "There is a tab in the indentation."},
    "fix.indent_tab": {
        "es": "Usa 4 espacios por nivel; los tabuladores no están permitidos.",
        "en": "Use 4 spaces per level; tabs are not allowed.",
    },
    "tab": {"es": "Hay un tabulador en el código.", "en": "There is a tab in the code."},
    "fix.tab": {"es": "Usa espacios; los tabuladores no están permitidos.", "en": "Use spaces; tabs are not allowed."},
    "indent_size": {
        "es": "La indentación debe ser de 4 espacios por nivel (hay {n}).",
        "en": "Indentation must be 4 spaces per level (found {n}).",
    },
    "fix.indent_size": {
        "es": "Indenta este bloque con {expected} espacios.",
        "en": "Indent this block with {expected} spaces.",
    },
    "dedent_mismatch": {
        "es": "La indentación de esta línea no corresponde a ningún bloque abierto.",
        "en": "The indentation of this line does not match any open block.",
    },
    "fix.dedent_mismatch": {
        "es": "Alinéala con el bloque al que pertenece, en múltiplos de 4 espacios.",
        "en": "Align it with the block it belongs to, in multiples of 4 spaces.",
    },
    "unterminated_string": {"es": "El texto no está cerrado.", "en": "The text is not closed."},
    "fix.unterminated_string": {
        "es": 'Añade {quote} al final del texto.',
        "en": "Add {quote} at the end of the text.",
    },
    "bad_char": {"es": 'El carácter "{ch}" no es válido aquí.', "en": 'The character "{ch}" is not valid here.'},
    "fix.bad_char": {"es": "Bórralo o escríbelo dentro de un texto.", "en": "Remove it or write it inside a text."},
    "unknown_unit": {"es": '"{unit}" no es una unidad conocida.', "en": '"{unit}" is not a known unit.'},
    "fix.unknown_unit": {
        "es": "Separa el número del nombre con un espacio o usa una unidad válida como s, m, h, km, MB, % o EUR.",
        "en": "Separate the number from the name with a space or use a valid unit such as s, m, h, km, MB, % or EUR.",
    },
    "bad_number": {"es": 'El número "{text}" no está bien escrito.', "en": 'The number "{text}" is malformed.'},
    "fix.bad_number": {
        "es": "Escribe números como 1_000, 3.14 o 1e-3.",
        "en": "Write numbers like 1_000, 3.14 or 1e-3.",
    },
    "unclosed_bracket": {"es": 'El "{open}" no se cierra.', "en": 'The "{open}" is never closed.'},
    "fix.unclosed_bracket": {"es": 'Añade "{close}" donde termina.', "en": 'Add "{close}" where it ends.'},
    "unmatched_bracket": {"es": 'Sobra este "{ch}".', "en": 'This "{ch}" has no matching opening.'},
    "fix.unmatched_bracket": {
        "es": "Bórralo o abre el paréntesis que le corresponde.",
        "en": "Remove it or open the matching bracket.",
    },
    "empty_interp": {"es": "La interpolación {{}} está vacía.", "en": "The interpolation {{}} is empty."},
    "fix.empty_interp": {
        "es": "Escribe una expresión dentro, o {{{{ para una llave literal.",
        "en": "Write an expression inside, or {{{{ for a literal brace.",
    },
    # parser
    "unexpected": {"es": "No se esperaba {got} aquí.", "en": "Unexpected {got} here."},
    "fix.expected": {"es": "Se esperaba {expected}.", "en": "Expected {expected}."},
    "else_needs_var": {
        "es": "else en un try necesita un nombre para el error.",
        "en": "else in a try needs a name for the error.",
    },
    "fix.else_needs_var": {"es": "Escribe else e: y usa e.message.", "en": "Write else e: and use e.message."},
    "invalid_target": {"es": "No se puede asignar a esta expresión.", "en": "You cannot assign to this expression."},
    "fix.invalid_target": {
        "es": "Asigna a una variable, a un campo (x.campo) o a un elemento (x[i]).",
        "en": "Assign to a variable, a field (x.field) or an element (x[i]).",
    },
    "reserved_word": {
        "es": '"{word}" es una palabra reservada y no puede usarse como nombre.',
        "en": '"{word}" is a reserved word and cannot be used as a name.',
    },
    "fix.reserved_word": {"es": "Elige otro nombre, por ejemplo {word}_.", "en": "Choose another name, for example {word}_."},
    "duplicate_name": {"es": '"{name}" está repetido.', "en": '"{name}" is repeated.'},
    "fix.duplicate_name": {
        "es": "Cada parámetro, campo o variante necesita un nombre distinto.",
        "en": "Each parameter, field or variant needs a different name.",
    },
    "type_mixed": {
        "es": 'El type "{name}" mezcla campos y variantes.',
        "en": 'The type "{name}" mixes fields and variants.',
    },
    "fix.type_mixed": {
        "es": "Un type es un registro (campos) o un enum (variantes); sepáralo en dos types.",
        "en": "A type is either a record (fields) or an enum (variants); split it into two types.",
    },
    "pipe_needs_group": {"es": "{step} {col} necesita un group by antes.", "en": "{step} {col} needs a group by first."},
    "fix.pipe_needs_group": {
        "es": "Para el total de una columna usa map {col} |> {step}.",
        "en": "For the total of a column use map {col} |> {step}.",
    },
    "pipe_group_next": {
        "es": "Después de group by solo puede ir sum, count o avg.",
        "en": "Only sum, count or avg can follow group by.",
    },
    "fix.pipe_group_next": {
        "es": "Añade |> sum columna, |> count o |> avg columna.",
        "en": "Add |> sum column, |> count or |> avg column.",
    },
    "pipe_step_arg": {"es": "El paso {step} necesita {what}.", "en": "The step {step} needs {what}."},
    "fix.pipe_step_arg": {"es": "Escríbelo así: |> {example}.", "en": "Write it like this: |> {example}."},
    # checker (compile errors)
    "ignored_error": {
        "es": 'La llamada a "{name}" puede fallar y su error se ignora.',
        "en": 'The call to "{name}" can fail and its error is ignored.',
    },
    "fix.ignored_error": {
        "es": "Añade ? al final para pasarlo al llamador, o gestiónalo con try y else e.",
        "en": "Add ? at the end to pass it to the caller, or handle it with try and else e.",
    },
    "question_outside_fn": {
        "es": "? solo se puede usar dentro de una función que devuelve T!.",
        "en": "? can only be used inside a function that returns T!.",
    },
    "fix.question_outside_fn": {
        "es": "Aquí no hay llamador: gestiona el error con try y else e.",
        "en": "There is no caller here: handle the error with try and else e.",
    },
    "fallible_needs_bang": {
        "es": '"{fn}" puede fallar, pero su tipo de retorno {type} no termina en !.',
        "en": '"{fn}" can fail, but its return type {type} does not end in !.',
    },
    "fix.fallible_needs_bang": {"es": "Escríbelo como {type}!.", "en": "Write it as {type}!."},
    "pub_fallible_untyped": {
        "es": 'La función pub "{fn}" puede fallar, pero no declara su tipo de retorno.',
        "en": 'The pub function "{fn}" can fail, but does not declare its return type.',
    },
    "fix.pub_fallible_untyped": {
        "es": "Añade -> T! antes de los dos puntos, con el tipo que devuelve.",
        "en": "Add -> T! before the colon, with the type it returns.",
    },
    "pub_param_type": {
        "es": 'El parámetro "{param}" de la función pub "{fn}" no tiene tipo.',
        "en": 'Parameter "{param}" of the pub function "{fn}" has no type.',
    },
    "fix.pub_param_type": {
        "es": "Las funciones pub declaran sus tipos: escribe {param}: tipo.",
        "en": "pub functions declare their types: write {param}: type.",
    },
    "pub_return_type": {
        "es": 'La función pub "{fn}" devuelve un valor pero no declara su tipo.',
        "en": 'The pub function "{fn}" returns a value but does not declare its type.',
    },
    "fix.pub_return_type": {
        "es": "Añade -> tipo antes de los dos puntos.",
        "en": "Add -> type before the colon.",
    },
    "question_not_error": {
        "es": "? solo sirve para valores que pueden fallar (T!).",
        "en": "? only works on values that can fail (T!).",
    },
    "fix.question_not_error": {
        "es": "Para valores que pueden faltar (T?) usa ?? con un valor por defecto.",
        "en": "For values that may be missing (T?) use ?? with a default.",
    },
    "question_cannot_fail": {
        "es": '"{name}" no puede fallar, así que ? no tiene nada que pasar.',
        "en": '"{name}" cannot fail, so ? has nothing to pass up.',
    },
    "fix.question_cannot_fail": {"es": "Quita el ?.", "en": "Remove the ?."},
    "coalesce_on_error": {
        "es": '?? solo sirve para valores que pueden faltar (T?), pero "{name}" puede fallar (T!).',
        "en": '?? only works on values that may be missing (T?), but "{name}" can fail (T!).',
    },
    "fix.coalesce_on_error": {
        "es": "Usa ? para pasar el error o try y else e para gestionarlo.",
        "en": "Use ? to pass the error up or try and else e to handle it.",
    },
    "ensure_outside_fn": {
        "es": "{kw} solo se puede usar dentro de una función.",
        "en": "{kw} can only be used inside a function.",
    },
    "fix.ensure_outside_fn": {
        "es": "Muévelo al principio del cuerpo de la función.",
        "en": "Move it to the start of the function body.",
    },
    "old_outside_ensure": {
        "es": "old() solo se puede usar dentro de ensure.",
        "en": "old() can only be used inside ensure.",
    },
    "fix.old_outside_ensure": {
        "es": "Escribe ensure ... == old(expresión) al principio de la función.",
        "en": "Write ensure ... == old(expression) at the start of the function.",
    },
    "return_outside_fn": {
        "es": "return solo se puede usar dentro de una función.",
        "en": "return can only be used inside a function.",
    },
    "fix.return_outside_fn": {
        "es": "Quítalo, o mueve este código a una función.",
        "en": "Remove it, or move this code into a function.",
    },
    # runtime diagnostics
    "name_not_found": {"es": '"{name}" no existe.', "en": '"{name}" does not exist.'},
    "fix.name_not_found": {"es": "Defínelo antes de usarlo.", "en": "Define it before using it."},
    "task_write_var": {
        "es": 'Una tarea no puede cambiar "{name}": es de fuera de la tarea, y otras tareas pueden usarla a la vez.',
        "en": 'A task cannot change "{name}": it belongs outside the task, and other tasks may use it at the same time.',
    },
    "fix.task_write_var": {
        "es": 'Devuelve el resultado con return (en parallel) o envíalo por un channel (en spawn).',
        "en": 'Return the result with return (in parallel) or send it through a channel (in spawn).',
    },
    "record_shared": {
        "es": 'Una tarea no puede cambiar un {type} creado antes de que empezara: otras tareas pueden estar leyéndolo.',
        "en": 'A task cannot change a {type} created before it started: other tasks may be reading it.',
    },
    "fix.record_shared": {
        "es": 'Crea el {type} dentro de la tarea, o devuelve el cambio con return y aplícalo cuando termine el parallel.',
        "en": 'Create the {type} inside the task, or return the change and apply it when the parallel ends.',
    },
    "parallel_sibling": {
        "es": '"{name}" lo calcula otra tarea de este parallel, y las tareas se ejecutan a la vez.',
        "en": '"{name}" is computed by another task of this parallel, and the tasks run at the same time.',
    },
    "fix.parallel_sibling": {
        "es": 'Usa "{name}" después del bloque parallel.',
        "en": 'Use "{name}" after the parallel block.',
    },
    "parallel_twice": {
        "es": 'Dos tareas de este parallel calculan "{name}".',
        "en": 'Two tasks of this parallel compute "{name}".',
    },
    "fix.parallel_twice": {
        "es": 'Cada tarea debe calcular un nombre distinto.',
        "en": 'Each task must compute a different name.',
    },
    "parallel_return": {
        "es": 'return no puede usarse dentro de un bloque parallel:.',
        "en": 'return cannot be used inside a parallel: block.',
    },
    "fix.parallel_return": {
        "es": 'Asigna el resultado a un nombre y úsalo después del bloque.',
        "en": 'Assign the result to a name and use it after the block.',
    },
    "channel_closed": {
        "es": 'No se puede enviar por un channel cerrado.',
        "en": 'You cannot send through a closed channel.',
    },
    "fix.channel_closed": {
        "es": 'Envía todos los valores antes de cerrarlo con close().',
        "en": 'Send every value before closing it with close().',
    },
    "deadlock": {
        "es": 'Todas las tareas están esperando en un channel y ninguna puede continuar.',
        "en": 'Every task is waiting on a channel and none can go on.',
    },
    "fix.deadlock": {
        "es": 'Cierra el channel con close() cuando no haya nada más que enviar, o envía lo que otra tarea espera.',
        "en": 'Close the channel with close() when there is nothing more to send, or send what another task waits for.',
    },
    "spawn_read": {
        "es": 'Una tarea de spawn no puede leer "{name}": es una variable del programa que otras tareas pueden cambiar.',
        "en": 'A spawned task cannot read "{name}": it is a variable of the program that other tasks may change.',
    },
    "fix.spawn_read": {
        "es": 'Pásale el valor como argumento o por un channel, o usa shared.',
        "en": 'Pass the value as an argument or through a channel, or use shared.',
    },
    "not_a_channel": {
        "es": 'select espera channels, no un valor de tipo {vtype}.',
        "en": 'select waits on channels, not on a value of type {vtype}.',
    },
    "fix.not_a_channel": {
        "es": 'Escribe nombre from canal, donde canal es un channel(...).',
        "en": 'Write name from c, where c is a channel(...).',
    },
    "spawn_failed": {
        "es": 'No se pudo iniciar la tarea.',
        "en": 'The task could not be started.',
    },
    "fix.spawn_failed": {
        "es": 'Hay demasiadas tareas a la vez; espera a que terminen algunas.',
        "en": 'There are too many tasks at once; wait for some of them to end.',
    },
    "weak_place": {
        "es": "weak solo puede marcar un campo de un type.",
        "en": "weak can only mark a field of a type.",
    },
    "fix.weak_place": {
        "es": "Escribe weak en el tipo de un campo, por ejemplo parent: weak Node?",
        "en": "Write weak in the type of a field, for example parent: weak Node?",
    },
    "weak_optional": {
        "es": "Un campo weak se queda sin valor cuando su destino se libera.",
        "en": "A weak field loses its value when its target is freed.",
    },
    "fix.weak_optional": {
        "es": "Escríbelo como opcional: {name}: weak {type}?",
        "en": "Make it optional: {name}: weak {type}?",
    },
    "weak_target": {
        "es": '"{type}" no es un type con campos: weak solo se usa con registros.',
        "en": '"{type}" is not a type with fields: weak only applies to records.',
    },
    "fix.weak_target": {
        "es": "Quita weak: esos valores no forman ciclos.",
        "en": "Remove weak: those values cannot form cycles.",
    },
    "type_cycle": {
        "es": "Los types {cycle} se refieren unos a otros: forman un ciclo de referencias que nunca se liberaría.",
        "en": "The types {cycle} refer to each other: they form a reference cycle that would never be freed.",
    },
    "fix.type_cycle": {
        "es": "Marca uno de los campos como weak, por ejemplo {field}: weak {type}?",
        "en": "Mark one of the fields as weak, for example {field}: weak {type}?",
    },
    "immutable": {
        "es": '"{name}" es inmutable y no se puede cambiar.',
        "en": '"{name}" is immutable and cannot be changed.',
    },
    "fix.immutable": {
        "es": "Decláralo con var {name} = ... si necesitas cambiarlo.",
        "en": "Declare it with var {name} = ... if it needs to change.",
    },
    "already_declared": {
        "es": '"{name}" ya está declarado en este bloque.',
        "en": '"{name}" is already declared in this block.',
    },
    "fix.already_declared": {
        "es": "Usa otro nombre, o cámbialo con {name} = ... sin var.",
        "en": "Use another name, or change it with {name} = ... without var.",
    },
    "type_mismatch": {
        "es": '"{name}" es de tipo {type} pero recibe {value} ({vtype}).',
        "en": '"{name}" has type {type} but gets {value} ({vtype}).',
    },
    "fix.type_mismatch": {
        "es": "Pasa un valor de tipo {type} o cambia el tipo declarado.",
        "en": "Pass a value of type {type} or change the declared type.",
    },
    "fix.type_mismatch_union": {
        "es": "Usa uno de estos valores: {values}.",
        "en": "Use one of these values: {values}.",
    },
    "return_mismatch": {
        "es": '"{fn}" debe devolver {type} pero devuelve {value} ({vtype}).',
        "en": '"{fn}" must return {type} but returns {value} ({vtype}).',
    },
    "fix.return_mismatch": {
        "es": "Devuelve un valor de tipo {type} o cambia el tipo de retorno.",
        "en": "Return a value of type {type} or change the return type.",
    },
    "none_not_allowed": {
        "es": '"{name}" es de tipo {type} y no puede ser none.',
        "en": '"{name}" has type {type} and cannot be none.',
    },
    "fix.none_not_allowed": {
        "es": "Si puede faltar, decláralo como {type}?.",
        "en": "If it may be missing, declare it as {type}?.",
    },
    "unknown_type": {"es": '"{name}" no es un tipo conocido.', "en": '"{name}" is not a known type.'},
    "fix.unknown_type": {"es": "Defínelo con type {name}: o usa un tipo existente.", "en": "Define it with type {name}: or use an existing type."},
    "mix_numbers": {"es": "No se pueden mezclar {a} y {b}.", "en": "{a} and {b} cannot be mixed."},
    "fix.mix_numbers": {
        "es": "Convierte uno de los dos con decimal(x) o float(x).",
        "en": "Convert one of them with decimal(x) or float(x).",
    },
    "currency_mismatch": {
        "es": "No se puede operar con money en monedas distintas ({a} y {b}).",
        "en": "You cannot operate on money in different currencies ({a} and {b}).",
    },
    "fix.currency_mismatch": {
        "es": "Convierte antes con convert(x, to: {a}, rate: ...).",
        "en": "Convert first with convert(x, to: {a}, rate: ...).",
    },
    "unit_mismatch": {
        "es": "No se pueden combinar {a} y {b}: miden cosas distintas.",
        "en": "{a} and {b} cannot be combined: they measure different things.",
    },
    "fix.unit_mismatch": {"es": "Usa unidades de la misma magnitud.", "en": "Use units of the same dimension."},
    "bad_operands": {
        "es": "No se puede aplicar {op} a {a} y {b}.",
        "en": "Cannot apply {op} to {a} and {b}.",
    },
    "fix.bad_operands": {
        "es": "Convierte los valores a tipos compatibles.",
        "en": "Convert the values to compatible types.",
    },
    "bad_operand": {"es": "No se puede aplicar {op} a {a}.", "en": "Cannot apply {op} to {a}."},
    "fix.bad_operand": {"es": "Usa un valor numérico.", "en": "Use a numeric value."},
    "not_bool": {"es": "{what} debe ser bool, pero es {vtype}.", "en": "{what} must be a bool, but it is {vtype}."},
    "fix.not_bool": {
        "es": "Escribe una comparación explícita, por ejemplo x != 0 o len(x) > 0.",
        "en": "Write an explicit comparison, for example x != 0 or len(x) > 0.",
    },
    "not_callable": {"es": "Un valor {vtype} no se puede llamar.", "en": "A {vtype} value cannot be called."},
    "fix.not_callable": {
        "es": "Solo se pueden llamar funciones y tipos.",
        "en": "Only functions and types can be called.",
    },
    "arg_missing": {
        "es": 'Falta el argumento "{param}" en la llamada a "{fn}".',
        "en": 'The argument "{param}" is missing in the call to "{fn}".',
    },
    "fix.arg_missing": {"es": "Pásalo así: {fn}(..., {param}: valor).", "en": "Pass it like this: {fn}(..., {param}: value)."},
    "arg_unknown": {
        "es": '"{fn}" no tiene ningún parámetro "{param}".',
        "en": '"{fn}" has no parameter "{param}".',
    },
    "fix.arg_unknown": {"es": "Parámetros válidos: {params}.", "en": "Valid parameters: {params}."},
    "arg_too_many": {
        "es": '"{fn}" recibe como mucho {n} argumentos, pero se le pasan {m}.',
        "en": '"{fn}" takes at most {n} arguments, but {m} are given.',
    },
    "fix.arg_too_many": {"es": "Quita los argumentos que sobran.", "en": "Remove the extra arguments."},
    "arg_twice": {
        "es": 'El argumento "{param}" se pasa dos veces.',
        "en": 'The argument "{param}" is passed twice.',
    },
    "fix.arg_twice": {"es": "Pásalo una sola vez.", "en": "Pass it only once."},
    "positional_after_named": {
        "es": "Un argumento sin nombre no puede ir después de uno con nombre.",
        "en": "An unnamed argument cannot come after a named one.",
    },
    "fix.positional_after_named": {
        "es": "Pon primero los argumentos sin nombre.",
        "en": "Put the unnamed arguments first.",
    },
    "no_member": {
        "es": '{vtype} no tiene ningún campo ni método "{name}".',
        "en": '{vtype} has no field or method "{name}".',
    },
    "fix.no_member": {"es": "Revisa la definición de {vtype}.", "en": "Check the definition of {vtype}."},
    "none_member": {"es": 'El valor es none y no tiene "{name}".', "en": 'The value is none and has no "{name}".'},
    "fix.none_member": {
        "es": "Si puede faltar, da un valor por defecto con ?? antes de usarlo.",
        "en": "If it may be missing, give it a default with ?? before using it.",
    },
    "readonly_member": {
        "es": '"{name}" de {vtype} no se puede cambiar.',
        "en": '"{name}" of {vtype} cannot be changed.',
    },
    "fix.readonly_member": {
        "es": "Crea un valor nuevo en lugar de modificarlo.",
        "en": "Create a new value instead of modifying it.",
    },
    "not_indexable": {"es": "Un valor {vtype} no se puede indexar con [].", "en": "A {vtype} value cannot be indexed with []."},
    "fix.not_indexable": {"es": "Usa [] con listas, mapas y textos.", "en": "Use [] with lists, maps and texts."},
    "bad_index": {"es": "El índice de {vtype} debe ser int, pero es {itype}.", "en": "The index of a {vtype} must be an int, but it is {itype}."},
    "fix.bad_index": {"es": "Usa un número entero, empezando en 0.", "en": "Use a whole number, starting at 0."},
    "not_iterable": {"es": "Un valor {vtype} no se puede recorrer.", "en": "A {vtype} value cannot be iterated."},
    "fix.not_iterable": {
        "es": "Recorre una lista, un mapa, un conjunto, un rango o un texto.",
        "en": "Iterate over a list, a map, a set, a range or a text.",
    },
    "no_match": {"es": "Ningún caso del match encaja con {value}.", "en": "No match arm fits {value}."},
    "fix.no_match": {"es": "Añade un caso _: al final.", "en": "Add a _: arm at the end."},
    "trait_missing": {
        "es": '"{type}" dice ser {trait} pero no define fn {method}.',
        "en": '"{type}" says it is {trait} but does not define fn {method}.',
    },
    "fix.trait_missing": {"es": "Añade fn {method} a {type}.", "en": "Add fn {method} to {type}."},
    "not_a_trait": {"es": '"{name}" no es un trait.', "en": '"{name}" is not a trait.'},
    "fix.not_a_trait": {"es": "Después de is va el nombre de un trait.", "en": "After is comes the name of a trait."},
    "field_missing": {
        "es": 'Falta el campo "{field}" al crear {type}.',
        "en": 'The field "{field}" is missing when creating {type}.',
    },
    "fix.field_missing": {"es": "Pásalo así: {type}(..., {field}: valor).", "en": "Pass it like this: {type}(..., {field}: value)."},
    "not_a_type_value": {"es": "Después de is debe ir un tipo, pero {vtype} no lo es.", "en": "is must be followed by a type, but {vtype} is not one."},
    "fix.not_a_type_value": {"es": "Escribe x is int, x is User o e is NotFound.", "en": "Write x is int, x is User or e is NotFound."},
    "variant_fields": {
        "es": '"{name}" tiene {n} campos, pero el patrón tiene {m}.',
        "en": '"{name}" has {n} fields, but the pattern has {m}.',
    },
    "fix.variant_fields": {"es": "Escribe un patrón por campo.", "en": "Write one pattern per field."},
    "not_supported": {
        "es": "{what} no está disponible en la fase 0 de Nexo.",
        "en": "{what} is not available in phase 0 of Nexo.",
    },
    "fix.not_supported": {
        "es": "Este prototipo solo incluye el núcleo del lenguaje; quítalo para ejecutar el programa.",
        "en": "This prototype only includes the core of the language; remove it to run the program.",
    },
    "bad_argument": {"es": "{fn}: {detail}.", "en": "{fn}: {detail}."},
    "fix.bad_argument": {"es": "Revisa los argumentos de {fn}.", "en": "Check the arguments of {fn}."},
    "unhashable": {"es": "Un valor {vtype} no puede ser clave ni elemento de un set.", "en": "A {vtype} value cannot be a key or a set element."},
    "fix.unhashable": {
        "es": "Usa textos, números, bool, money o variantes sin datos como claves.",
        "en": "Use texts, numbers, bools, money or variants without data as keys.",
    },
    "csv_value": {"es": "No se puede guardar {vtype} en un CSV.", "en": "{vtype} cannot be saved to a CSV."},
    "fix.csv_value": {"es": "Guarda una lista de filas (mapas) o de registros.", "en": "Save a list of rows (maps) or records."},
    "compare": {"es": "No se pueden comparar {a} y {b}.", "en": "{a} and {b} cannot be compared."},
    "fix.compare": {"es": "Compara valores del mismo tipo.", "en": "Compare values of the same type."},
    # tests
    "assert_failed": {"es": "La aserción no se cumple: {expr}{detail}.", "en": "The assertion does not hold: {expr}{detail}."},
    "fix.assert_failed": {"es": "Corrige el código o el valor esperado.", "en": "Fix the code or the expected value."},
    "assert_not_failed": {"es": "Se esperaba que {expr} fallara, pero no falló.", "en": "{expr} was expected to fail, but it did not."},
    "fix.assert_not_failed": {
        "es": "Revisa que la función devuelva un error en este caso.",
        "en": "Check that the function returns an error in this case.",
    },
    "no_tests": {"es": "No hay bloques test en {path}.", "en": "There are no test blocks in {path}."},
    # nexo lint (D86)
    "header_warning": {
        "es": "Aviso en {file}, línea {line}:",
        "en": "Warning in {file}, line {line}:",
    },
    "lint.unused": {
        "es": "\"{name}\" se asigna pero nunca se usa.",
        "en": "\"{name}\" is assigned but never used.",
    },
    "fix.lint.unused": {
        "es": "Bórralo o úsalo.",
        "en": "Remove it or use it.",
    },
    "lint.var_never_changed": {
        "es": "\"{name}\" se declara con var pero nunca cambia.",
        "en": "\"{name}\" is declared with var but never changes.",
    },
    "fix.lint.var_never_changed": {
        "es": "Quita var: un nombre que no cambia es inmutable.",
        "en": "Remove var: a name that does not change is immutable.",
    },
    "lint.loop_var": {
        "es": "\"{name}\" no se usa dentro del bucle.",
        "en": "\"{name}\" is not used inside the loop.",
    },
    "fix.lint.loop_var": {
        "es": "Usa repeat {count} para repetir el bloque.",
        "en": "Use repeat {count} to repeat the block.",
    },
    "lint.compare_bool": {
        "es": "Comparar con true o false sobra.",
        "en": "Comparing with true or false is redundant.",
    },
    "fix.lint.compare_bool": {
        "es": "Escribe {suggestion}.",
        "en": "Write {suggestion}.",
    },
    "lint.double_not": {
        "es": "Dos not seguidos se anulan.",
        "en": "Two nots in a row cancel out.",
    },
    "fix.lint.double_not": {
        "es": "Quita los dos not.",
        "en": "Remove both nots.",
    },
    "lint.self_assign": {
        "es": "\"{name}\" se asigna a sí mismo.",
        "en": "\"{name}\" is assigned to itself.",
    },
    "fix.lint.self_assign": {
        "es": "Borra esta línea.",
        "en": "Remove this line.",
    },
    "lint.unreachable": {
        "es": "Este código nunca se ejecuta.",
        "en": "This code never runs.",
    },
    "fix.lint.unreachable": {
        "es": "Va después de return o fail(); bórralo o muévelo antes.",
        "en": "It comes after return or fail(); remove it or move it earlier.",
    },
    "lint.constant_condition": {
        "es": "La condición de {word} es siempre {value}.",
        "en": "The condition of {word} is always {value}.",
    },
    "fix.lint.constant_condition": {
        "es": "Usa una condición que pueda cambiar, o quita el {word}.",
        "en": "Use a condition that can change, or remove the {word}.",
    },
    "lint.unused_use": {
        "es": "use {name} no se usa.",
        "en": "use {name} is not used.",
    },
    "fix.lint.unused_use": {
        "es": "Borra esta línea.",
        "en": "Remove this line.",
    },
    "lint.useless_expr": {
        "es": "El valor de esta expresión no se usa.",
        "en": "The value of this expression is not used.",
    },
    "fix.lint.useless_expr": {
        "es": "Guárdalo en una variable, pásalo a una función o bórralo.",
        "en": "Store it in a variable, pass it to a function or remove it.",
    },
    "lint.lazy_pipeline": {
        "es": "Este pipeline termina en {step}, así que nunca se ejecuta.",
        "en": "This pipeline ends in {step}, so it never runs.",
    },
    "fix.lint.lazy_pipeline": {
        "es": "Guarda el resultado o termina con |> each, |> save(...), |> count o |> sum.",
        "en": "Store the result or end it with |> each, |> save(...), |> count or |> sum.",
    },
    "lint.duplicate_key": {
        "es": "La clave {text} está repetida en este mapa.",
        "en": "The key {text} is repeated in this map.",
    },
    "fix.lint.duplicate_key": {
        "es": "Una clave repetida sustituye a la anterior; deja solo una.",
        "en": "A repeated key replaces the earlier one; keep only one.",
    },
    "lint.unused_binding": {
        "es": "\"{name}\" se captura en el patrón pero no se usa.",
        "en": "\"{name}\" is bound by the pattern but never used.",
    },
    "fix.lint.unused_binding": {
        "es": "Escribe _ en su lugar.",
        "en": "Write _ instead.",
    },
    "lint_summary": {
        "es": "Avisos: {n}. Arreglos seguros con nexo fix: {safe}.",
        "en": "Warnings: {n}. Safe fixes with nexo fix: {safe}.",
    },
    "lint_none": {
        "es": "No hay avisos en {path}.",
        "en": "No warnings in {path}.",
    },
    # failures (error values). The first sentence becomes e.message.
    "unhandled": {"es": "Error sin gestionar{kind}: {message}", "en": "Unhandled error{kind}: {message}"},
    "fix.unhandled": {"es": "Gestiónalo con try y else e.", "en": "Handle it with try and else e."},
    "err.NotFound": {"es": 'No se encuentra el archivo "{path}"', "en": 'File "{path}" not found'},
    "fix.NotFound": {
        "es": "Comprueba la ruta, o gestiónalo con try y else e is NotFound.",
        "en": "Check the path, or handle it with try and else e is NotFound.",
    },
    "err.IOError": {"es": 'No se puede acceder a "{path}": {reason}', "en": 'Cannot access "{path}": {reason}'},
    "fix.IOError": {
        "es": "Comprueba que la ruta exista y que tengas permiso, o gestiónalo con try y else e is IOError.",
        "en": "Check that the path exists and that you have permission, or handle it with try and else e is IOError.",
    },
    "err.InvalidData": {"es": "Datos no válidos: {detail}", "en": "Invalid data: {detail}"},
    "fix.InvalidData": {
        "es": "Corrige los datos de entrada, o gestiónalo con try y else e is InvalidData.",
        "en": "Fix the input data, or handle it with try and else e is InvalidData.",
    },
    "err.Overflow": {"es": "El resultado no cabe en un int de 64 bits", "en": "The result does not fit in a 64-bit int"},
    "fix.Overflow": {
        "es": "Usa valores más pequeños, o decimal(x) o float(x) para números grandes.",
        "en": "Use smaller values, or decimal(x) or float(x) for large numbers.",
    },
    "err.DivisionByZero": {"es": "División por cero", "en": "Division by zero"},
    "fix.DivisionByZero": {
        "es": "Comprueba que el divisor no sea 0 antes de dividir.",
        "en": "Check that the divisor is not 0 before dividing.",
    },
    "err.OutOfRange": {
        "es": "El índice {index} está fuera de los límites (tamaño {size})",
        "en": "Index {index} is out of bounds (size {size})",
    },
    "fix.OutOfRange": {
        "es": "Usa un índice entre 0 y el tamaño menos 1.",
        "en": "Use an index between 0 and the size minus 1.",
    },
    "err.ContractBroken": {"es": "No se cumple {kw} {expr}{values}", "en": "{kw} {expr} does not hold{values}"},
    "fix.ContractBroken": {
        "es": "Revisa los valores que recibe la función o corrige su cuerpo.",
        "en": "Check the values the function receives or fix its body.",
    },
    "err.Timeout": {"es": "El bloque tardó más de {limit}", "en": "The block took longer than {limit}"},
    "fix.Timeout": {
        "es": "Aumenta el límite de within, o gestiónalo con try y else e is Timeout.",
        "en": "Raise the within limit, or handle it with try and else e is Timeout.",
    },
    "err.InvalidValue": {"es": "{detail}", "en": "{detail}"},
    "fix.InvalidValue": {
        "es": "Comprueba el valor antes de usarlo, o gestiónalo con try y else e is InvalidValue.",
        "en": "Check the value before using it, or handle it with try and else e is InvalidValue.",
    },
    "too_deep": {
        "es": "Demasiadas llamadas anidadas (recursión infinita).",
        "en": "Too many nested calls (infinite recursion).",
    },
    "fix.too_deep": {
        "es": "Comprueba que la recursión tenga un caso base que la detenga.",
        "en": "Check that the recursion has a base case that stops it.",
    },
    # details used inside error values and argument errors
    "detail.not_a": {"es": '"{value}" no es un valor {type} válido', "en": '"{value}" is not a valid {type}'},
    "detail.out_of_range": {"es": "{value} está fuera del rango {range}", "en": "{value} is outside the range {range}"},
    "detail.missing_field": {"es": 'falta el campo "{field}" de {type}', "en": 'the field "{field}" of {type} is missing'},
    "detail.field_type": {"es": 'el campo "{field}" debe ser {type}, pero es {vtype}', "en": 'the field "{field}" must be {type}, but it is {vtype}'},
    "detail.expected": {"es": "se esperaba {type}, pero hay {vtype}", "en": "expected {type}, but found {vtype}"},
    "detail.json": {"es": "el JSON no es válido (línea {line}, columna {col})", "en": "the JSON is not valid (line {line}, column {col})"},
    "detail.encoding": {"es": 'el archivo "{path}" no está en UTF-8', "en": 'the file "{path}" is not UTF-8'},
    "detail.expects": {"es": "espera {what}, pero recibe {vtype}", "en": "expects {what}, but gets {vtype}"},
    "detail.use_validate": {
        "es": "no convierte texto; usa validate(x, as: {type})?",
        "en": "does not convert text; use validate(x, as: {type})?",
    },
    "detail.positive": {"es": "el número de partes debe ser mayor que 0", "en": "the number of parts must be greater than 0"},
    "detail.step_zero": {"es": "el paso no puede ser 0", "en": "the step cannot be 0"},
    "detail.mode": {"es": "mode debe ser half_even o half_up", "en": "mode must be half_even or half_up"},
}


def msg(key: str, **args) -> str:
    entry = MESSAGES[key]
    return entry[LANG].format(**args)


def has(key: str) -> bool:
    return key in MESSAGES


class NexoError(Exception):
    """A diagnostic: compile errors and runtime errors that are not values.

    It always ends the program (or the test) and is never caught by `try`.
    """

    def __init__(self, key: str, span: Span | None = None, fix: str | None = None, **args):
        super().__init__(key)
        self.key = key
        self.span = span
        self.fix = fix  # fix key without the "fix." prefix; defaults to key
        self.args_ = args

    def text(self) -> str:
        what = msg(self.key, **self.args_)
        fix_key = "fix." + (self.fix or self.key)
        if has(fix_key):
            return f"{what} {msg(fix_key, **self.args_)}"
        return what

    def __str__(self) -> str:  # useful in unit tests
        return self.text()


def render(span: Span | None, text: str, header: str = "header") -> str:
    """Render a message in the section 14 format (warnings of `nexo lint`
    use the header "Warning in...")."""
    if span is None:
        return f"Error: {text}\n"
    out = [msg(header, file=span.file, line=span.line)]
    source = SOURCES.get(span.file)
    if source is not None:
        lines = source.splitlines()
        if 1 <= span.line <= len(lines):
            line = lines[span.line - 1]
            stripped = line.lstrip(" \t")
            cut = len(line) - len(stripped)
            stripped = stripped.rstrip()
            start = max(0, span.col - 1 - cut)
            end = min(len(stripped), span.end_col - 1 - cut)
            if end <= start:
                end = start + 1
            out.append("  " + stripped)
            out.append("  " + " " * start + "^" * (end - start))
    out.append(text)
    return "\n".join(out) + "\n"


def suggest(name: str, candidates) -> str | None:
    """Closest candidate by Damerau-Levenshtein distance, or None."""
    best, best_d = None, None
    limit = 1 if len(name) <= 4 else 2
    for cand in candidates:
        if cand == name or cand.startswith("__"):
            continue
        d = _distance(name, cand)
        if d <= limit and (best_d is None or d < best_d):
            best, best_d = cand, d
    return best


def _distance(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 2:
        return 99
    prev2 = None
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[len(b)]
