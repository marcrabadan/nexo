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
    "lsp_fix_all": {"es": "Aplica los arreglos seguros (nexo fix)", "en": "Apply the safe fixes (nexo fix)"},
    "check_ok": {"es": "No hay errores en {path}.", "en": "No errors in {path}."},
    # phase 4 (D89-)
    "err.Error.python": {"es": "{type} en Python: {message}", "en": "{type} in Python: {message}"},
    "fix.Error.python": {"es": "Revisa los argumentos de la llamada, o gestiónalo con try y else e.", "en": "Check the arguments of the call, or handle it with try and else e."},
    "python_module": {"es": "No se puede importar el módulo de Python \"{name}\" ({reason}).", "en": "The Python module \"{name}\" cannot be imported ({reason})."},
    "fix.python_module": {"es": "Instálalo con pip en el Python que usa Nexo, o revisa su nombre.", "en": "Install it with pip in the Python that Nexo uses, or check its name."},
    "python_value": {"es": "Un valor {vtype} no se puede pasar a Python.", "en": "A {vtype} value cannot be passed to Python."},
    "fix.python_value": {"es": "Pasa números, textos, listas, mapas, conjuntos o registros; convierte lo demás con str().", "en": "Pass numbers, texts, lists, maps, sets or records; convert the rest with str()."},
    "python_missing": {"es": "Para usar Python hace falta su biblioteca compartida (libpython), y no se encuentra.", "en": "Using Python needs its shared library (libpython), and it cannot be found."},
    "fix.python_missing": {"es": "Instala el paquete de desarrollo de Python o indica cuál usar con la variable de entorno NEXO_PYTHON.", "en": "Install Python's development package or say which Python to use with the environment variable NEXO_PYTHON."},
    "c_library": {"es": "No se encuentra la biblioteca de C \"{name}\".", "en": "The C library \"{name}\" cannot be found."},
    "fix.c_library": {"es": "Instala la biblioteca (y su paquete de desarrollo, con la cabecera) o escribe su ruta: use c \"./lib/milib.so\" as milib.", "en": "Install the library (and its development package, with the header) or write its path: use c \"./lib/mylib.so\" as mylib."},
    "c_header": {"es": "No se encuentra la cabecera de \"{name}\" (se buscó {tried}).", "en": "The header of \"{name}\" cannot be found (looked for {tried})."},
    "fix.c_header": {"es": "Instala su paquete de desarrollo o pon su archivo .h junto al programa.", "en": "Install its development package or put its .h file next to the program."},
    "c_compiler": {"es": "Para leer las cabeceras de C hace falta un compilador de C (cc, clang o gcc).", "en": "Reading C headers needs a C compiler (cc, clang or gcc)."},
    "fix.c_compiler": {"es": "Instala uno o indica cuál usar con la variable de entorno CC.", "en": "Install one or say which one to use with the environment variable CC."},
    "c_type": {"es": "{fn} usa el tipo \"{type}\", que Nexo no puede pasar a C.", "en": "{fn} uses the type \"{type}\", which Nexo cannot pass to C."},
    "fix.c_type": {"es": "Solo se pasan int, float, bool y textos (const char *); envuelve la función en otra de C con esos tipos.", "en": "Only int, float, bool and texts (const char *) are passed; wrap the function in another C function with those types."},
    "c_variadic": {"es": "{fn} recibe un número variable de argumentos, que Nexo no puede pasar a C.", "en": "{fn} takes a variable number of arguments, which Nexo cannot pass to C."},
    "fix.c_variadic": {"es": "Envuélvela en una función de C con argumentos fijos.", "en": "Wrap it in a C function with fixed arguments."},
    "c_args": {"es": "{fn} tiene demasiados argumentos para llamarla desde Nexo.", "en": "{fn} has too many arguments to be called from Nexo."},
    "fix.c_args": {"es": "Nexo pasa hasta 6 argumentos enteros y 8 decimales; envuélvela en una función de C con menos.", "en": "Nexo passes up to 6 integer and 8 floating arguments; wrap it in a C function with fewer."},
    "c_symbol": {"es": "\"{name}\" está en la cabecera pero no en la biblioteca {lib}.", "en": "\"{name}\" is in the header but not in the library {lib}."},
    "fix.c_symbol": {"es": "Comprueba que la biblioteca es la de esa cabecera.", "en": "Check that the library is the one of that header."},
    "err.AIError.steps": {"es": "{agent} no termina en {n} pasos", "en": "{agent} does not finish in {n} steps"},
    "fix.AIError.steps": {"es": "Sube max_steps o haz el objetivo más concreto.", "en": "Raise max_steps or make the goal more concrete."},
    "ai_not_tool": {"es": "\"{name}\" no es una herramienta.", "en": "\"{name}\" is not a tool."},
    "fix.ai_not_tool": {"es": "Márcala con @tool \"lo que hace\" antes de su fn.", "en": "Mark it with @tool \"what it does\" before its fn."},
    "agent_key": {"es": "\"{option}\" no es una opción de un agent.", "en": "\"{option}\" is not an option of an agent."},
    "fix.agent_key": {"es": "Usa model, tools, goal, max_steps o confirm.", "en": "Use model, tools, goal, max_steps or confirm."},
    "agent_confirm": {"es": "\"{name}\" está en confirm pero no en tools.", "en": "\"{name}\" is in confirm but not in tools."},
    "fix.agent_confirm": {"es": "Añádela a tools o quítala de confirm.", "en": "Add it to tools or remove it from confirm."},
    "info.ai_confirm": {"es": "¿Usar {call}? (s/n): ", "en": "Use {call}? (y/n): "},
    "info.ai_setup_provider": {"es": "Proveedor (anthropic, openai u ollama): ", "en": "Provider (anthropic, openai or ollama): "},
    "info.ai_setup_model": {"es": "Modelo (por ejemplo claude-opus): ", "en": "Model (for example claude-opus): "},
    "info.ai_setup_key": {"es": "Variable de entorno con la clave (por ejemplo ANTHROPIC_KEY): ", "en": "Environment variable that holds the key (for example ANTHROPIC_KEY): "},
    "info.ai_setup_url": {"es": "Dirección (Intro para la de siempre): ", "en": "Address (Enter for the usual one): "},
    "info.ai_saved": {"es": "Configuración guardada en {path}", "en": "Configuration saved in {path}"},
    "info.ai_usage_row": {"es": "{day} {model}: {calls} llamadas, {input} tokens de entrada, {output} de salida", "en": "{day} {model}: {calls} calls, {input} input tokens, {output} output"},
    "info.ai_usage_none": {"es": "Todavía no hay llamadas a ask.", "en": "There are no calls to ask yet."},
    "ai_setup_var": {"es": "\"{name}\" no es un nombre de variable de entorno.", "en": "\"{name}\" is not the name of an environment variable."},
    "fix.ai_setup_var": {"es": "Escribe solo letras, números y _, por ejemplo ANTHROPIC_KEY; la clave nunca se guarda en un archivo.", "en": "Write only letters, digits and _, for example ANTHROPIC_KEY; the key is never saved in a file."},
    "ai_key": {"es": "\"{option}\" no es una opción de un bloque ai.", "en": "\"{option}\" is not an option of an ai block."},
    "err.AIError": {"es": "{model} no da una respuesta válida: {detail}", "en": "{model} gives no valid answer: {detail}"},
    "fix.AIError": {"es": "Prueba otra vez u otro modelo (fallback = [...]), o gestiónalo con try y else e is AIError.", "en": "Try again or another model (fallback = [...]), or handle it with try and else e is AIError."},
    "err.AIError.setup": {"es": "No hay ningún proveedor de IA configurado. Ejecuta: nexo ai setup", "en": "No AI provider is configured. Run: nexo ai setup"},
    "fix.AIError.setup": {"es": "Añade un bloque ai: con provider y model, o guarda una configuración con nexo ai setup.", "en": "Add an ai: block with provider and model, or save a configuration with nexo ai setup."},
    "err.AIError.key": {"es": "Falta la clave de {provider}", "en": "The {provider} key is missing"},
    "fix.AIError.key": {"es": "Añade key = secret(\"NOMBRE\") al bloque ai o al perfil.", "en": "Add key = secret(\"NAME\") to the ai block or the profile."},
    "detail.ai_no_text": {"es": "la respuesta no tiene texto", "en": "the answer has no text"},
    "fix.ai_key": {"es": "Usa provider, model, key, url o fallback.", "en": "Use provider, model, key, url or fallback."},
    "ai_provider": {"es": "\"{provider}\" no es un proveedor de IA.", "en": "\"{provider}\" is not an AI provider."},
    "fix.ai_provider": {"es": "Usa \"anthropic\", \"openai\" u \"ollama\".", "en": "Use \"anthropic\", \"openai\" or \"ollama\"."},
    "ai_model": {"es": "\"{model}\" no es un modelo: escribe \"proveedor:modelo\" o un nombre de ai models.", "en": "\"{model}\" is not a model: write \"provider:model\" or a name of ai models."},
    "fix.ai_model": {"es": "Por ejemplo \"anthropic:claude-opus\" u \"ollama:llama\".", "en": "For example \"anthropic:claude-opus\" or \"ollama:llama\"."},
    "ai_option": {"es": "{option} no acepta {value}.", "en": "{option} does not accept {value}."},
    "fix.ai_option": {"es": "Usa {values}.", "en": "Use {values}."},
    "ai_profile": {"es": "No hay ningún perfil de IA \"{name}\".", "en": "There is no AI profile \"{name}\"."},
    "fix.ai_profile": {"es": "Decláralo con un bloque ai profile antes de usarlo.", "en": "Declare it with an ai profile block before using it."},
    "ai_toml": {"es": "La línea {line} de {path} no se entiende.", "en": "Line {line} of {path} cannot be read."},
    "fix.ai_toml": {"es": "Escribe nombre = \"texto\" o nombre = [\"a\", \"b\"].", "en": "Write name = \"text\" or name = [\"a\", \"b\"]."},
    "ai_stream_typed": {"es": "Una respuesta con tipo no se puede recibir por partes.", "en": "A typed answer cannot be received in pieces."},
    "fix.ai_stream_typed": {"es": "Quita stream: true o el tipo de la variable.", "en": "Remove stream: true or the type of the variable."},
    "fix.DatabaseError.unique": {"es": "Ese campo es @unique: usa otro valor, o gestiónalo con try y else e is DatabaseError.", "en": "That field is @unique: use another value, or handle it with try and else e is DatabaseError."},
    "fix.NotFound.secret": {"es": "Define esa variable de entorno antes de ejecutar el programa.", "en": "Set that environment variable before running the program."},
    "rate_unit": {"es": "\"{unit}\" no es una unidad de tiempo para un límite.", "en": "\"{unit}\" is not a unit of time for a limit."},
    "fix.rate_unit": {"es": "Escribe per second, per minute, per hour, per day o per month.", "en": "Write per second, per minute, per hour, per day or per month."},
    "rate_per": {"es": "Un límite se cuenta por ip o por user, no por \"{per}\".", "en": "A limit is counted per ip or per user, not per \"{per}\"."},
    "fix.rate_per": {"es": "Escribe, por ejemplo, 10 per minute per ip.", "en": "Write, for example, 10 per minute per ip."},
    "role_needs_auth": {"es": "@role necesita que el server {server} tenga auth.", "en": "@role needs the server {server} to have auth."},
    "fix.role_needs_auth": {"es": "Añade auth = jwt(key: secret(\"JWT_KEY\")) al server.", "en": "Add auth = jwt(key: secret(\"JWT_KEY\")) to the server."},
    "route_twice": {"es": "La ruta {method} {path} está repetida.", "en": "The route {method} {path} is repeated."},
    "fix.route_twice": {"es": "Deja una sola ruta para cada método y camino.", "en": "Keep a single route for each method and path."},
    "web_status": {"es": "{status} no es un código de estado HTTP.", "en": "{status} is not an HTTP status code."},
    "fix.web_status": {"es": "Devuelve un número entre 100 y 599 (204, 404...), o el valor que se envía como JSON.", "en": "Return a number between 100 and 599 (204, 404...), or the value to send as JSON."},
    "web_port": {"es": "El server {server} no puede escuchar en el puerto {port}: {reason}.", "en": "The server {server} cannot listen on port {port}: {reason}."},
    "fix.web_port": {"es": "Usa otro puerto con port = ... o la variable de entorno PORT.", "en": "Use another port with port = ... or the environment variable PORT."},
    "detail.bad_token": {"es": "el token no es válido o ha caducado", "en": "the token is not valid or has expired"},
    "detail.csrf": {"es": "la petición viene de otro origen", "en": "the request comes from another origin"},
    "detail.need_token": {"es": "falta un token válido en Authorization: Bearer", "en": "a valid token is missing in Authorization: Bearer"},
    "detail.need_role": {"es": "hace falta el rol {role}", "en": "the role {role} is needed"},
    "detail.need_body": {"es": "falta el cuerpo JSON de la petición", "en": "the JSON body of the request is missing"},
    "detail.need_param": {"es": "falta el parámetro {name}", "en": "the parameter {name} is missing"},
    "info.serving": {"es": "{server} escucha en http://127.0.0.1:{port}", "en": "{server} listens on http://127.0.0.1:{port}"},
    "db_no_target": {"es": "{file} no tiene connect(\"...\"), así que no se sabe qué base de datos usar.", "en": "{file} has no connect(\"...\"), so the database to use is unknown."},
    "fix.db_no_target": {"es": "Indícala con --db ruta.db.", "en": "Give it with --db path.db."},
    "info.db_diff_none": {"es": "{db} ya coincide con los types de {file}.", "en": "{db} already matches the types of {file}."},
    "info.db_diff_written": {"es": "Migración escrita en {path}:", "en": "Migration written to {path}:"},
    "info.db_migration_header": {"es": "Migración {n} de {db}, hecha por nexo db diff con {file}.", "en": "Migration {n} of {db}, made by nexo db diff from {file}."},
    "info.db_migration_drops": {"es": "Esta migración borra datos: aplícala con nexo db migrate --allow-drop.", "en": "This migration drops data: apply it with nexo db migrate --allow-drop."},
    "info.db_type_changed": {"es": "{table}.{column} es {was} en la base de datos y {now} en el type: cámbialo a mano.", "en": "{table}.{column} is {was} in the database and {now} in the type: change it by hand."},
    "info.db_migrated": {"es": "Aplicada {path}.", "en": "Applied {path}."},
    "info.db_up_to_date": {"es": "{db} está al día.", "en": "{db} is up to date."},
    "db_needs_drop": {"es": "La migración {path} borra columnas o tablas.", "en": "The migration {path} drops columns or tables."},
    "fix.db_needs_drop": {"es": "Revísala y aplícala con nexo db migrate --allow-drop.", "en": "Review it and apply it with nexo db migrate --allow-drop."},
    "err.DatabaseError": {"es": "Error en la base de datos {db}: {reason}", "en": "Database error in {db}: {reason}"},
    "fix.DatabaseError": {"es": "Revisa la consulta y la base de datos, o gestiónalo con try y else e is DatabaseError.", "en": "Check the query and the database, or handle it with try and else e is DatabaseError."},
    "err.DatabaseError.unique": {"es": "Ya hay un {type} con {field} = {value}", "en": "There is already a {type} with {field} = {value}"},
    "err.NotFound.row": {"es": "No hay ningún {type} con {conditions}", "en": "There is no {type} with {conditions}"},
    "fix.NotFound.row": {"es": "Comprueba los valores que buscas, o gestiónalo con try y else e is NotFound.", "en": "Check the values you look for, or handle it with try and else e is NotFound."},
    "table_field": {"es": "El campo \"{field}\" de {type} es {ftype}, que no se puede guardar en una tabla.", "en": "The field \"{field}\" of {type} is {ftype}, which cannot be stored in a table."},
    "fix.table_field": {"es": "Usa int, float, bool, str, decimal, money, date, time, un enum sin datos o ref T; una lista de registros se escribe many T.", "en": "Use int, float, bool, str, decimal, money, date, time, an enum without data or ref T; a list of records is written many T."},
    "table_needs_ref": {"es": "El campo \"{field}\" de {type} guarda un {target}.", "en": "The field \"{field}\" of {type} holds a {target}."},
    "fix.table_needs_ref": {"es": "Escribe {field}: ref {target} para guardar una referencia a su fila.", "en": "Write {field}: ref {target} to store a reference to its row."},
    "db_unsaved": {"es": "Este {type} no está guardado en la base de datos.", "en": "This {type} is not saved in the database."},
    "fix.db_unsaved": {"es": "Guárdalo antes con db.{type}.add(...) o léelo de la base de datos.", "en": "Save it first with db.{type}.add(...) or read it from the database."},
    "db_value": {"es": "Un valor {vtype} no se puede guardar en la base de datos.", "en": "A {vtype} value cannot be stored in the database."},
    "fix.db_value": {"es": "Usa números, textos, bool, decimal, money, date, time o registros guardados.", "en": "Use numbers, texts, bools, decimal, money, date, time or saved records."},
    "sql_literal": {"es": "db.sql necesita el texto de la consulta escrito en la llamada.", "en": "db.sql needs the text of the query written in the call."},
    "fix.sql_literal": {"es": "Escribe db.sql(\"SELECT ... {{valor}}\"): los valores entre llaves se pasan como parámetros, nunca dentro del texto.", "en": "Write db.sql(\"SELECT ... {{value}}\"): the values in braces are passed as parameters, never inside the text."},
    "table_no_id": {"es": "El type \"{type}\" no tiene un campo id.", "en": "The type \"{type}\" has no id field."},
    "fix.table_no_id": {"es": "Añade id: id para poder actualizar o borrar sus filas.", "en": "Add id: id to update or remove its rows."},
    "table_many": {"es": "{type}.{field} es many {target}, pero {target} no tiene un campo ref {type}.", "en": "{type}.{field} is many {target}, but {target} has no ref {type} field."},
    "fix.table_many": {"es": "Añade a {target} un campo como owner: ref {type}.", "en": "Add a field such as owner: ref {type} to {target}."},
    "detail.find_needs": {"es": "necesita al menos una condición, por ejemplo find(id: 1)", "en": "needs at least one condition, for example find(id: 1)"},
    "err.NetworkError": {"es": "No se puede conectar con {url}: {reason}", "en": "Cannot reach {url}: {reason}"},
    "fix.NetworkError": {"es": "Comprueba la dirección y la conexión, o gestiónalo con try y else e is NetworkError.", "en": "Check the address and the connection, or handle it with try and else e is NetworkError."},
    "err.HTTPError": {"es": "{method} {url} respondió {status}", "en": "{method} {url} answered {status}"},
    "fix.HTTPError": {"es": "Comprueba la petición y sus permisos, o gestiónalo con try y else e is HTTPError.", "en": "Check the request and its permissions, or handle it with try and else e is HTTPError."},
    "detail.bad_url": {"es": "la dirección debe empezar por http:// o https://", "en": "the address must start with http:// or https://"},
    "detail.redirects": {"es": "demasiadas redirecciones", "en": "too many redirects"},
    "detail.timeout": {"es": "no responde a tiempo", "en": "it does not answer in time"},
    "detail.refused": {"es": "rechaza la conexión", "en": "it refuses the connection"},
    "detail.unknown_host": {"es": "el nombre del servidor no existe", "en": "the server name does not exist"},
    "detail.no_connection": {"es": "no hay conexión", "en": "there is no connection"},
    "detail.bad_key": {"es": "el texto no es una clave {what} en base64", "en": "the text is not an {what} key in base64"},
    "secret_literal": {"es": "Hay una clave escrita en el código.", "en": "There is a key written in the code."},
    "fix.secret_literal": {"es": "Léela con secret(\"NOMBRE\") y define la variable de entorno NOMBRE fuera del código.", "en": "Read it with secret(\"NAME\") and set the environment variable NAME outside the code."},
    "secret_shown": {"es": "Un secreto no se puede mostrar ni convertir en texto.", "en": "A secret cannot be shown or turned into text."},
    "fix.secret_shown": {"es": "Pasa el secreto solo donde se necesita (key: ..., una contraseña de connect, una cabecera de http); nunca a print, str() ni a un archivo.", "en": "Pass the secret only where it is needed (key: ..., a connect password, an http header); never to print, str() or a file."},
    "err.NotFound.secret": {"es": "El secreto \"{name}\" no está definido", "en": "The secret \"{name}\" is not set"},
    "detail.not_encrypted": {"es": "los datos no están cifrados con esta clave o se han modificado", "en": "the data is not encrypted with this key or has been changed"},
    "bad_regex": {"es": "\"{pattern}\" no es una expresión regular válida.", "en": "\"{pattern}\" is not a valid regular expression."},
    "fix.bad_regex": {"es": "Revisa los paréntesis, corchetes y escapes; no hay lookarounds como (?=...) ni referencias como \\1.", "en": "Check the brackets and escapes; there are no lookarounds such as (?=...) nor backreferences such as \\1."},
    "detail.template": {"es": "la plantilla {path} usa {{{name}}}, que no tiene valor", "en": "the template {path} uses {{{name}}}, which has no value"},
    "detail.not_a_file": {"es": "no es un archivo", "en": "it is not a file"},
    "date_days": {"es": "Una fecha solo avanza días enteros, y {value} no lo es.", "en": "A date only moves by whole days, and {value} is not."},
    "fix.date_days": {"es": "Suma días (2 days) a una fecha, o usa un time (now + 3h) para horas y minutos.", "en": "Add days (2 days) to a date, or use a time (now + 3h) for hours and minutes."},
    "detail.zone": {"es": "\"{zone}\" no es una zona horaria conocida", "en": "\"{zone}\" is not a known time zone"},
    "detail.date_range": {"es": "la fecha queda fuera de los años 1 a 9999", "en": "the date falls outside the years 1 to 9999"},
    "detail.bad_date": {"es": "{value} no es una fecha válida", "en": "{value} is not a valid date"},
    "not_supported": {"es": "{what} no está disponible en la fase 5 de Nexo.", "en": "{what} is not available in phase 5 of Nexo."},
    "fix.not_supported": {"es": "Esta versión incluye el núcleo, la biblioteca estándar, nexo.sync, nexo.ai, nexo.web, nexo.db, nexo.infra, nexo.ci, nexo.stream, los flags y la privacidad; quítalo para ejecutar el programa.", "en": "This version includes the core, the standard library, nexo.sync, nexo.ai, nexo.web, nexo.db, nexo.infra, nexo.ci, nexo.stream, flags and privacy; remove it to run the program."},
    "use_needs_as": {"es": "\"{name}\" no es un nombre válido para usarlo en el código.", "en": "\"{name}\" is not a valid name to use in the code."},
    "fix.use_needs_as": {"es": "Dale un nombre con as, por ejemplo use c \"{name}\" as lib.", "en": "Give it a name with as, for example use c \"{name}\" as lib."},
    "table_only": {"es": "{word} solo se usa en los campos de un type marcado con @table.", "en": "{word} is only used in the fields of a type marked with @table."},
    "fix.table_only": {"es": "Escribe @table antes del type (y use nexo.db al principio del archivo), o quita {word}.", "en": "Write @table before the type (and use nexo.db at the start of the file), or remove {word}."},
    "needs_use": {"es": "{what} es parte de {module}, que no está cargado.", "en": "{what} belongs to {module}, which is not loaded."},
    "fix.needs_use": {"es": "Añade use {module} al principio del archivo.", "en": "Add use {module} at the start of the file."},
    "table_two_ids": {"es": "El type \"{type}\" tiene dos campos de tipo id.", "en": "The type \"{type}\" has two fields of type id."},
    "fix.table_two_ids": {"es": "Una tabla tiene una sola clave primaria; deja un solo campo id.", "en": "A table has a single primary key; keep only one id field."},
    "transaction_return": {"es": "return no puede salir de un bloque transaction.", "en": "return cannot leave a transaction block."},
    "fix.transaction_return": {"es": "Guarda el resultado en una variable y haz return después del bloque.", "en": "Store the result in a variable and return after the block."},
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
    "step_return": {"es": "Un step no puede usar return.", "en": "A step cannot use return."},
    "fix.step_return": {"es": "Los pasos de un pipeline siguen en orden hasta el final; quita el return o lleva el código a una función.", "en": "The steps of a pipeline go on in order to the end; remove the return or move the code into a function."},
    "flag_entry": {"es": "Cada línea de un bloque flags es nombre = valor.", "en": "Each line of a flags block is name = value."},
    "fix.flag_entry": {"es": "Escríbela así: nuevo_pago = 10%.", "en": "Write it like this: new_checkout = 10%."},
    "flag_value": {"es": "El valor del flag \"{name}\" no es válido.", "en": "The value of the flag \"{name}\" is not valid."},
    "fix.flag_value": {"es": "Usa on, off, un porcentaje entre 0% y 100% o users where condición.", "en": "Use on, off, a percentage between 0% and 100% or users where condition."},
    "flag_option": {"es": "\"{option}\" no es una opción de un flag.", "en": "\"{option}\" is not an option of a flag."},
    "fix.flag_option": {"es": "La única opción es expires: fecha (por ejemplo expires: 2027-03-01).", "en": "The only option is expires: date (for example expires: 2027-03-01)."},
    "flag_unknown": {"es": "El flag \"{name}\" no está declarado.", "en": "The flag \"{name}\" is not declared."},
    "fix.flag_unknown": {"es": "Decláralo en el bloque flags: con su valor, por ejemplo {name} = off.", "en": "Declare it in the flags: block with its value, for example {name} = off."},
    "flag_needs_user": {"es": "El flag \"{name}\" depende del usuario, y flag() no lo recibe.", "en": "The flag \"{name}\" depends on the user, and flag() does not get one."},
    "fix.flag_needs_user": {"es": "Pásale el usuario: flag(\"{name}\", user).", "en": "Pass it the user: flag(\"{name}\", user)."},
    "flag_set_value": {"es": "\"{value}\" no es un valor para un flag.", "en": "\"{value}\" is not a value for a flag."},
    "fix.flag_set_value": {"es": "Usa on, off o un porcentaje como 50%.", "en": "Use on, off or a percentage such as 50%."},
    "lint.flag_expired": {"es": "El flag \"{name}\" caducó el {date}.", "en": "The flag \"{name}\" expired on {date}."},
    "fix.lint.flag_expired": {"es": "Quita el flag del bloque flags y deja en el código solo el camino que se queda.", "en": "Remove the flag from the flags block and keep only the path that stays in the code."},
    "info.flags_set": {"es": "{name} = {value} (etapa {stage})", "en": "{name} = {value} (stage {stage})"},
    "which_file": {"es": "No sé qué archivo leer: escribe el que tiene el bloque {word}.", "en": "I do not know which file to read: name the one with the {word} block."},
    "fix.which_file": {"es": "Por ejemplo: nexo plan app.nexo.", "en": "For example: nexo plan app.nexo."},
    "type_setting": {"es": "\"{name}\" no es un ajuste de un type.", "en": "\"{name}\" is not a setting of a type."},
    "fix.type_setting": {"es": "Un type solo declara keep = duración after campo y purpose = \"finalidad\".", "en": "A type only declares keep = duration after field and purpose = \"purpose\"."},
    "keep_form": {"es": "keep se escribe como una duración después de un campo de fecha.", "en": "keep is written as a duration after a date field."},
    "fix.keep_form": {"es": "Escríbelo así: keep = 5 years after last_activity, con last_activity un campo date o time que no sea @private.", "en": "Write it like this: keep = 5 years after last_activity, with last_activity a date or time field that is not @private."},
    "purpose_text": {"es": "purpose necesita un texto.", "en": "purpose needs a text."},
    "fix.purpose_text": {"es": "Escríbelo así: purpose = \"facturación y envíos\".", "en": "Write it like this: purpose = \"billing and shipping\"."},
    "privacy_key": {"es": "La tabla {table} tiene campos @private y la base de datos no tiene clave para cifrarlos.", "en": "The table {table} has @private fields and the database has no key to encrypt them."},
    "fix.privacy_key": {"es": "Ábrela con una clave: connect(\"{db}\", key: secret(\"DB_KEY\")).", "en": "Open it with a key: connect(\"{db}\", key: secret(\"DB_KEY\"))."},
    "privacy_wrong_key": {"es": "Los campos @private de la tabla {table} no se pueden descifrar con esta clave.", "en": "The @private fields of the table {table} cannot be decrypted with this key."},
    "fix.privacy_wrong_key": {"es": "Abre la base de datos con la clave con la que se guardaron.", "en": "Open the database with the key they were saved with."},
    "privacy_ref": {"es": "El campo {field} de {type} es una referencia y no puede ser @private.", "en": "The field {field} of {type} is a reference and cannot be @private."},
    "fix.privacy_ref": {"es": "Marca @private los campos con los datos, no la referencia a otra tabla.", "en": "Mark @private the fields that hold the data, not the reference to another table."},
    "lint.privacy_keep": {"es": "El type \"{type}\" tiene datos personales y no dice cuánto tiempo se guardan.", "en": "The type \"{type}\" has personal data and does not say how long it is kept."},
    "fix.lint.privacy_keep": {"es": "Añade keep = 5 years after un_campo_de_fecha (en una compilación release es un error).", "en": "Add keep = 5 years after a_date_field (in a release build it is an error)."},
    "lint.privacy_purpose": {"es": "El type \"{type}\" tiene datos personales y no dice para qué se usan.", "en": "The type \"{type}\" has personal data and does not say what it is used for."},
    "fix.lint.privacy_purpose": {"es": "Añade purpose = \"para qué se usan\" (en una compilación release es un error).", "en": "Add purpose = \"what it is used for\" (in a release build it is an error)."},
    "privacy_release": {"es": "El type \"{type}\" tiene datos personales sin {missing}.", "en": "The type \"{type}\" has personal data without {missing}."},
    "fix.privacy_release": {"es": "En una compilación release todo type con @personal o @private declara keep y purpose (sección 23).", "en": "In a release build every type with @personal or @private declares keep and purpose (section 23)."},
    "info.privacy_title": {"es": "Datos personales de {file}:", "en": "Personal data of {file}:"},
    "info.privacy_none": {"es": "No hay campos @personal ni @private.", "en": "There are no @personal or @private fields."},
    "info.privacy_table": {"es": "tabla {table} en {db}", "en": "table {table} in {db}"},
    "info.privacy_memory": {"es": "no se guarda en una base de datos", "en": "not stored in a database"},
    "info.privacy_personal": {"es": "personal", "en": "personal"},
    "info.privacy_private": {"es": "sensible, cifrado en reposo", "en": "sensitive, encrypted at rest"},
    "info.privacy_keep": {"es": "se guarda: {keep}", "en": "kept: {keep}"},
    "info.privacy_purpose": {"es": "finalidad: {purpose}", "en": "purpose: {purpose}"},
    "info.privacy_readers": {"es": "lo leen: {readers}", "en": "read by: {readers}"},
    "info.privacy_program": {"es": "el programa", "en": "the program"},
    "info.privacy_missing": {"es": "(sin declarar)", "en": "(not declared)"},
    "info.privacy_reader": {"es": "{identity} (infra {infra}, database {database})", "en": "{identity} (infra {infra}, database {database})"},
    "stream_window": {"es": "{step} necesita una duración mayor que cero.", "en": "{step} needs a duration greater than zero."},
    "fix.stream_window": {"es": "Escríbelo así: |> {step} 1m.", "en": "Write it like this: |> {step} 1m."},
    "stream_window_source": {"es": "{step} solo sirve en un pipeline que empieza en un stream.", "en": "{step} only works in a pipeline that starts at a stream."},
    "fix.stream_window_source": {"es": "Empieza el pipeline en una fuente, por ejemplo kafka(\"clicks\") |> {step} 1m.", "en": "Start the pipeline at a source, for example kafka(\"clicks\") |> {step} 1m."},
    "stream_window_first": {"es": "{step} tiene que ser el primer paso después del stream.", "en": "{step} has to be the first step after the stream."},
    "fix.stream_window_first": {"es": "Pon la ventana justo después de la fuente y el resto de pasos detrás.", "en": "Put the window right after the source and the other steps after it."},
    "info.stream_dead": {"es": "{stream}: el mensaje {id} falla tras 4 intentos y pasa a {stream}.dead: {error}", "en": "{stream}: message {id} fails after 4 tries and goes to {stream}.dead: {error}"},
    "info.stream_listening": {"es": "Escuchando {streams} (Ctrl-C para parar)", "en": "Listening to {streams} (Ctrl-C to stop)"},
    "stream_call": {"es": "Un pipeline sobre un stream solo admite los pasos de la sección 6 (where, map, pick, group by, sum, count, avg, sort by, take, each).", "en": "A pipeline over a stream only takes the steps of section 6 (where, map, pick, group by, sum, count, avg, sort by, take, each)."},
    "fix.stream_call": {"es": "Llama a la función dentro de each: |> each f.", "en": "Call the function inside each: |> each f."},
    "infra_key": {"es": "\"{key}\" no es un ajuste de {kind}.", "en": "\"{key}\" is not a setting of {kind}."},
    "fix.infra_key": {"es": "Revisa el nombre; la sección 11 lista los recursos y sus ajustes.", "en": "Check the name; section 11 lists the resources and their settings."},
    "infra_cloud": {"es": "La infra \"{infra}\" no dice dónde se despliega.", "en": "The infra \"{infra}\" does not say where it deploys."},
    "fix.infra_cloud": {"es": "Añade cloud = \"aws\", \"azure\", \"gcp\" o \"k8s\".", "en": "Add cloud = \"aws\", \"azure\", \"gcp\" or \"k8s\"."},
    "stage_entry": {"es": "Cada línea de un stage es clave = valor.", "en": "Each line of a stage is key = value."},
    "fix.stage_entry": {"es": "Escríbela así: size = large.", "en": "Write it like this: size = large."},
    "policy_rule": {"es": "\"{rule}\" no es una regla de la política de una infra.", "en": "\"{rule}\" is not a rule of the policy of an infra."},
    "fix.policy_rule": {"es": "Usa deny public tipo, require encryption, require tags [...] o allow personal data to \"zona\".", "en": "Use deny public kind, require encryption, require tags [...] or allow personal data to \"area\"."},
    "output_form": {"es": "Una salida se escribe output nombre = recurso.dato.", "en": "An output is written output name = resource.value."},
    "fix.output_form": {"es": "Por ejemplo: output url = api.url, o output db = customers.url, secret.", "en": "For example: output url = api.url, or output db = customers.url, secret."},
    "dns_form": {"es": "Un dns se escribe dns \"dominio\" -> app.", "en": "A dns is written dns \"domain\" -> app."},
    "fix.dns_form": {"es": "Por ejemplo: dns \"api.shop.com\" -> api, https.", "en": "For example: dns \"api.shop.com\" -> api, https."},
    "native_form": {"es": "Un recurso nativo se escribe native \"tipo\" nombre:.", "en": "A native resource is written native \"type\" name:."},
    "fix.native_form": {"es": "Por ejemplo: native \"aws_wafv2_web_acl\" firewall: y sus ajustes debajo.", "en": "For example: native \"aws_wafv2_web_acl\" firewall: and its settings below."},
    "resource_form": {"es": "Un {kind} se escribe {kind} nombre, con sus ajustes debajo.", "en": "A {kind} is written {kind} name, with its settings below."},
    "fix.resource_form": {"es": "Por ejemplo: {kind} principal: y debajo clave = valor.", "en": "For example: {kind} main: and key = value below."},
    "env_entry": {"es": "Cada línea de env es NOMBRE = valor.", "en": "Each line of env is NAME = value."},
    "fix.env_entry": {"es": "Por ejemplo: DB = customers.url o API_KEY = secret(\"API_KEY\").", "en": "For example: DB = customers.url or API_KEY = secret(\"API_KEY\")."},
    "env_value": {"es": "La variable {name} necesita un texto, un número, un secret o un dato de un recurso.", "en": "The variable {name} needs a text, a number, a secret or a value of a resource."},
    "fix.env_value": {"es": "Escribe el valor como texto, por ejemplo \"{name}\".", "en": "Write the value as a text, for example \"{name}\"."},
    "scale_form": {"es": "scale se escribe con un rango de réplicas.", "en": "scale is written with a range of replicas."},
    "fix.scale_form": {"es": "Por ejemplo: scale 3..50: y debajo cpu > 70%.", "en": "For example: scale 3..50: and cpu > 70% below."},
    "release_form": {"es": "release necesita una estrategia: rolling, canary o blue_green.", "en": "release needs a strategy: rolling, canary or blue_green."},
    "fix.release_form": {"es": "Escribe release rolling (la de por defecto).", "en": "Write release rolling (the default one)."},
    "infra_ref": {"es": "{key} necesita el nombre de un {kind} de la misma infra, y \"{name}\" no lo es.", "en": "{key} needs the name of a {kind} of the same infra, and \"{name}\" is not one."},
    "fix.infra_ref": {"es": "Declara ese {kind} en la infra o corrige el nombre.", "en": "Declare that {kind} in the infra or fix the name."},
    "infra_attr": {"es": "\"{ref}\" no es un dato de un recurso de esta infra ni de otra.", "en": "\"{ref}\" is not a value of a resource of this infra or another one."},
    "fix.infra_attr": {"es": "Escribe recurso.dato, o infra.recurso.dato para otra infra, por ejemplo customers.url.", "en": "Write resource.value, or infra.resource.value for another infra, for example customers.url."},
    "infra_cycle": {"es": "Las infras {names} usan datos unas de otras en círculo.", "en": "The infras {names} use values of each other in a circle."},
    "fix.infra_cycle": {"es": "Quita una de las referencias: una infra solo usa datos de las que se aplican antes.", "en": "Remove one of the references: an infra only uses values of the ones applied before it."},
    "infra_attr_name": {"es": "\"{ref}\": un {kind} no tiene ese dato.", "en": "\"{ref}\": a {kind} does not have that value."},
    "fix.infra_attr_name": {"es": "Usa uno de los datos que da un {kind} (url, host, id...).", "en": "Use one of the values a {kind} gives (url, host, id...)."},
    "infra_value": {"es": "{key} necesita {what}.", "en": "{key} needs {what}."},
    "fix.infra_value": {"es": "Corrige el valor de {key}.", "en": "Fix the value of {key}."},
    "detail.infra_text": {"es": "un texto", "en": "a text"},
    "detail.infra_cidr": {"es": "un rango de direcciones como \"10.0.0.0/16\"", "en": "an address range such as \"10.0.0.0/16\""},
    "detail.infra_int": {"es": "un número entero", "en": "a whole number"},
    "detail.infra_number": {"es": "un número mayor que cero", "en": "a number greater than zero"},
    "detail.infra_bool": {"es": "true o false", "en": "true or false"},
    "detail.infra_size": {"es": "un tamaño como 512 MB", "en": "a size such as 512 MB"},
    "detail.infra_count": {"es": "un número o un rango como 3..10", "en": "a number or a range such as 3..10"},
    "detail.infra_tags": {"es": "un mapa de textos", "en": "a map of texts"},
    "infra_word": {"es": "{key} solo admite {words}.", "en": "{key} only takes {words}."},
    "fix.infra_word": {"es": "Usa uno de esos valores.", "en": "Use one of those values."},
    "infra_required": {"es": "El {kind} {name} necesita {key}.", "en": "The {kind} {name} needs {key}."},
    "fix.infra_required": {"es": "Añade {key} = ... en su bloque.", "en": "Add {key} = ... in its block."},
    "infra_range": {"es": "El rango {range} es demasiado pequeño para sus subredes.", "en": "The range {range} is too small for its subnets."},
    "fix.infra_range": {"es": "Usa un rango de /19 o mayor, por ejemplo \"10.0.0.0/16\".", "en": "Use a range of /19 or larger, for example \"10.0.0.0/16\"."},
    "allow_form": {"es": "Una regla de red se escribe allow from origen to recurso, port: número.", "en": "A network rule is written allow from source to resource, port: number."},
    "fix.allow_form": {"es": "Por ejemplo: allow from internet to api, port: 443.", "en": "For example: allow from internet to api, port: 443."},
    "can_form": {"es": "Un permiso se escribe can read|write tipo nombre.", "en": "A permission is written can read|write kind name."},
    "fix.can_form": {"es": "Por ejemplo: can read storage photos (tipos: storage, queue, database, cache).", "en": "For example: can read storage photos (kinds: storage, queue, database, cache)."},
    "server_image": {"es": "En azure la imagen de un server es editor:oferta:sku:versión, y \"{image}\" no lo es.", "en": "On azure the image of a server is publisher:offer:sku:version, and \"{image}\" is not."},
    "fix.server_image": {"es": "Por ejemplo: image = \"Canonical:ubuntu-24_04-lts:server:latest\".", "en": "For example: image = \"Canonical:ubuntu-24_04-lts:server:latest\"."},
    "infra_region": {"es": "La infra \"{infra}\" en {cloud} necesita region.", "en": "The infra \"{infra}\" on {cloud} needs region."},
    "fix.infra_region": {"es": "Añade region = \"...\" (por ejemplo \"eu-west-1\", \"westeurope\" o \"europe-west1\").", "en": "Add region = \"...\" (for example \"eu-west-1\", \"westeurope\" or \"europe-west1\")."},
    "infra_target": {"es": "Un {kind} no tiene equivalente en {cloud}.", "en": "A {kind} has no equivalent on {cloud}."},
    "fix.infra_target": {"es": "Quítalo de esta infra o decláralo en una infra de otra nube (sección 11).", "en": "Remove it from this infra or declare it in an infra on another cloud (section 11)."},
    "infra_network": {"es": "No hay conexión con {url} ({reason}).", "en": "There is no connection to {url} ({reason})."},
    "fix.infra_network": {"es": "Revisa la red y la dirección de la API; nada se ha cambiado después del último paso mostrado.", "en": "Check the network and the address of the API; nothing was changed after the last step shown."},
    "infra_needs_network": {"es": "En aws un {kind} ({name}) necesita una network, y la infra no tiene exactamente una.", "en": "On aws a {kind} ({name}) needs a network, and the infra does not have exactly one."},
    "fix.infra_needs_network": {"es": "Declara una network en la infra, o escribe network = nombre en {name}.", "en": "Declare a network in the infra, or write network = name in {name}."},
    "infra_dev_only": {"es": "En k8s una database ({name}) es solo para desarrollo, no para la etapa prod.", "en": "On k8s a database ({name}) is for development only, not for the prod stage."},
    "fix.infra_dev_only": {"es": "Usa una database de una nube para prod, o otra etapa.", "en": "Use a cloud database for prod, or another stage."},
    "infra_secret": {"es": "El secreto {name} no está definido.", "en": "The secret {name} is not defined."},
    "fix.infra_secret": {"es": "Define la variable de entorno {name} antes de nexo apply.", "en": "Define the environment variable {name} before nexo apply."},
    "policy_public": {"es": "La política de {infra} prohíbe un {kind} público, y {name} lo es.", "en": "The policy of {infra} forbids a public {kind}, and {name} is one."},
    "fix.policy_public": {"es": "Escribe public = false en {name}.", "en": "Write public = false in {name}."},
    "policy_encryption": {"es": "La política de {infra} exige cifrado, y el {kind} {name} lo quita.", "en": "The policy of {infra} requires encryption, and the {kind} {name} turns it off."},
    "fix.policy_encryption": {"es": "Quita encryption = false de {name}.", "en": "Remove encryption = false from {name}."},
    "policy_tags": {"es": "La política de {infra} exige etiquetas que el {kind} {name} no tiene: {tags}.", "en": "The policy of {infra} requires tags that the {kind} {name} lacks: {tags}."},
    "fix.policy_tags": {"es": "Añade tags = {{\"...\": \"...\"}} a la infra o al recurso.", "en": "Add tags = {{\"...\": \"...\"}} to the infra or the resource."},
    "data_region": {"es": "{infra} guarda datos personales en {region}, fuera de la zona {area}.", "en": "{infra} keeps personal data in {region}, outside the area {area}."},
    "fix.data_region": {"es": "Usa una región de {area}, o permítelo en la política: allow personal data to \"zona\".", "en": "Use a region in {area}, or allow it in the policy: allow personal data to \"area\"."},
    "budget_form": {"es": "budget se escribe como una cantidad en EUR al mes.", "en": "budget is written as an amount of EUR per month."},
    "fix.budget_form": {"es": "Por ejemplo: budget = 300 EUR per month.", "en": "For example: budget = 300 EUR per month."},
    "over_budget": {"es": "El coste estimado de {infra} ({cost} EUR al mes) supera su presupuesto ({budget} EUR al mes).", "en": "The estimated cost of {infra} ({cost} EUR per month) exceeds its budget ({budget} EUR per month)."},
    "fix.over_budget": {"es": "Reduce tamaños o réplicas, o sube budget.", "en": "Reduce sizes or replicas, or raise budget."},
    "state_form": {"es": "state se escribe s3(\"cubo\").", "en": "state is written s3(\"bucket\")."},
    "fix.state_form": {"es": "Por ejemplo: state = s3(\"nexo-state-shop\"); sin state se guarda en .nexo/state.", "en": "For example: state = s3(\"nexo-state-shop\"); without state it is kept in .nexo/state."},
    "infra_none": {"es": "{file} no tiene ningún bloque infra.", "en": "{file} has no infra block."},
    "fix.infra_none": {"es": "Declara uno: infra nombre: con cloud = \"...\".", "en": "Declare one: infra name: with cloud = \"...\"."},
    "info.plan_header": {"es": "Plan de {infra} ({cloud}{region}, etapa {stage}):", "en": "Plan for {infra} ({cloud}{region}, stage {stage}):"},
    "info.plan_none": {"es": "Sin cambios.", "en": "No changes."},
    "info.plan_summary": {"es": "Crear: {create}, cambiar: {change}, borrar: {delete}.", "en": "To create: {create}, to change: {change}, to delete: {delete}."},
    "info.plan_cost": {"es": "Coste estimado: {cost} EUR al mes.", "en": "Estimated cost: {cost} EUR per month."},
    "info.plan_cost_budget": {"es": "Coste estimado: {cost} EUR al mes (presupuesto: {budget} EUR al mes).", "en": "Estimated cost: {cost} EUR per month (budget: {budget} EUR per month)."},
    "info.plan_availability": {"es": "Disponibilidad estimada: {pct} %.", "en": "Estimated availability: {pct} %."},
    "info.cost_header": {"es": "Coste de {infra} ({cloud}, etapa {stage}):", "en": "Cost of {infra} ({cloud}, stage {stage}):"},
    "info.cost_total": {"es": "Total estimado: {cost} EUR al mes.", "en": "Estimated total: {cost} EUR per month."},
    "infra_api": {"es": "{what}: la nube responde {status}: {reason}", "en": "{what}: the cloud answers {status}: {reason}"},
    "fix.infra_api": {"es": "Corrige el recurso o los permisos y vuelve a ejecutar nexo apply; el estado guarda lo que ya se hizo.", "en": "Fix the resource or the permissions and run nexo apply again; the state keeps what was already done."},
    "infra_timeout": {"es": "{what} no termina en 30 minutos.", "en": "{what} does not finish in 30 minutes."},
    "fix.infra_timeout": {"es": "Mira su estado en la consola de la nube y vuelve a ejecutar nexo apply.", "en": "Look at its status in the cloud's console and run nexo apply again."},
    "infra_credentials": {"es": "Falta la variable de entorno {name}.", "en": "The environment variable {name} is missing."},
    "fix.infra_credentials": {"es": "Defínela con las credenciales de la nube antes del comando (en CI, como secreto del pipeline).", "en": "Define it with the cloud's credentials before the command (in CI, as a secret of the pipeline)."},
    "apply_needs_yes": {"es": "En CI, nexo apply necesita --yes.", "en": "In CI, nexo apply needs --yes."},
    "fix.apply_needs_yes": {"es": "Ejecuta nexo apply --yes.", "en": "Run nexo apply --yes."},
    "state_locked": {"es": "El estado {where} está bloqueado por otro comando.", "en": "The state {where} is locked by another command."},
    "fix.state_locked": {"es": "Espera a que termine; si no hay ninguno en marcha, borra el bloqueo {where}.lock.", "en": "Wait for it to finish; if none is running, delete the lock {where}.lock."},
    "import_resource": {"es": "La infra {infra} no tiene ningún recurso {name}.", "en": "The infra {infra} has no resource {name}."},
    "fix.import_resource": {"es": "Escribe el nombre que tiene en el programa.", "en": "Write the name it has in the program."},
    "import_which": {"es": "{name} tiene varios recursos nativos: {natives}.", "en": "{name} has several native resources: {natives}."},
    "fix.import_which": {"es": "Escribe cuál: nexo import nube recurso.nativo id.", "en": "Write which one: nexo import cloud resource.native id."},
    "import_missing": {"es": "{what} con id {id} no existe en la nube.", "en": "{what} with id {id} does not exist in the cloud."},
    "fix.import_missing": {"es": "Comprueba el id (el identificador que usa la API de esa nube).", "en": "Check the id (the identifier the API of that cloud uses)."},
    "import_cloud": {"es": "Ninguna infra del programa se despliega en {cloud}.", "en": "No infra of the program deploys to {cloud}."},
    "fix.import_cloud": {"es": "Escribe la nube de una de sus infras: aws, azure, gcp o k8s.", "en": "Write the cloud of one of its infras: aws, azure, gcp or k8s."},
    "info.apply_confirm": {"es": "¿Aplicar estos cambios? Escribe sí para seguir: ", "en": "Apply these changes? Type yes to go on: "},
    "info.apply_cancelled": {"es": "No se ha cambiado nada.", "en": "Nothing was changed."},
    "info.apply_done": {"es": "Aplicado: {create} creados, {change} cambiados, {delete} borrados.", "en": "Applied: {create} created, {change} changed, {delete} deleted."},
    "info.secret_output": {"es": "(secreto)", "en": "(secret)"},
    "info.unknown_value": {"es": "(se sabrá al aplicar)", "en": "(known after apply)"},
    "info.destroy_none": {"es": "{infra} no tiene nada creado.", "en": "{infra} has nothing created."},
    "info.destroy_list": {"es": "Se borrarán {n} recursos de {infra}:", "en": "{n} resources of {infra} will be deleted:"},
    "info.destroy_confirm": {"es": "Para borrarlo todo escribe el nombre de la infra ({infra}): ", "en": "To delete everything type the name of the infra ({infra}): "},
    "info.destroy_done": {"es": "{infra}: {n} recursos borrados.", "en": "{infra}: {n} resources deleted."},
    "info.drift_header": {"es": "Cambios fuera de Nexo en {infra} ({cloud}, etapa {stage}):", "en": "Changes outside Nexo in {infra} ({cloud}, stage {stage}):"},
    "info.drift_gone": {"es": "ya no existe", "en": "no longer exists"},
    "ci_trigger": {"es": "\"{entry}\" no es un trigger de un pipeline.", "en": "\"{entry}\" is not a trigger of a pipeline."},
    "fix.ci_trigger": {"es": "Escribe trigger push, trigger push to rama o trigger pull_request.", "en": "Write trigger push, trigger push to branch or trigger pull_request."},
    "ci_secrets": {"es": "secrets es una lista de nombres de variables de entorno.", "en": "secrets is a list of names of environment variables."},
    "fix.ci_secrets": {"es": "Escribe secrets = [\"AWS_KEY\", \"DB_PASS\"].", "en": "Write secrets = [\"AWS_KEY\", \"DB_PASS\"]."},
    "ci_version": {"es": "\"{value}\" no vale como version de un pipeline.", "en": "\"{value}\" is not valid as the version of a pipeline."},
    "fix.ci_version": {"es": "Escribe version = auto o una versión como \"1.4.0\".", "en": "Write version = auto or a version such as \"1.4.0\"."},
    "ci_entry": {"es": "\"{entry}\" no es parte de un pipeline.", "en": "\"{entry}\" is not part of a pipeline."},
    "fix.ci_entry": {"es": "Un pipeline tiene trigger, secrets, version, step nombre: y after success: o after failure:.", "en": "A pipeline has trigger, secrets, version, step name: and after success: or after failure:."},
    "ci_no_steps": {"es": "El pipeline {name} no tiene ningún step.", "en": "The pipeline {name} has no step."},
    "fix.ci_no_steps": {"es": "Añade al menos uno: step nombre: con su código debajo.", "en": "Add at least one: step name: with its code below."},
    "ci_missing": {"es": "{name} no tiene valor en este paso: lo da un paso anterior que no se ha ejecutado.", "en": "{name} has no value in this step: an earlier step that has not run gives it."},
    "fix.ci_missing": {"es": "Ejecuta antes ese paso, o el pipeline entero con nexo ci run.", "en": "Run that step first, or the whole pipeline with nexo ci run."},
    "ci_value": {"es": "Un step guarda {name}, de tipo {vtype}, y solo puede pasar a los siguientes textos, números, bool, listas y mapas.", "en": "A step keeps {name}, of type {vtype}, and can only pass texts, numbers, bools, lists and maps to the next ones."},
    "fix.ci_value": {"es": "Guarda un texto o un mapa con lo que necesitan los pasos siguientes.", "en": "Keep a text or a map with what the next steps need."},
    "ci_outside": {"es": "{fn}() solo funciona en un step de un pipeline.", "en": "{fn}() only works in a step of a pipeline."},
    "fix.ci_outside": {"es": "Llámalo dentro de step nombre: y ejecútalo con nexo ci run.", "en": "Call it inside step name: and run it with nexo ci run."},
    "ci_git": {"es": "version = auto necesita un repositorio git.", "en": "version = auto needs a git repository."},
    "fix.ci_git": {"es": "Ejecuta nexo ci run dentro del repositorio, o escribe la versión: version = \"1.0.0\".", "en": "Run nexo ci run inside the repository, or write the version: version = \"1.0.0\"."},
    "ci_none": {"es": "{file} no tiene ningún bloque pipeline.", "en": "{file} has no pipeline block."},
    "fix.ci_none": {"es": "Declara uno: pipeline nombre: con sus step.", "en": "Declare one: pipeline name: with its steps."},
    "ci_which": {"es": "Hay varios pipelines: {names}.", "en": "There are several pipelines: {names}."},
    "fix.ci_which": {"es": "Indica cuál: nexo ci run nombre.", "en": "Say which one: nexo ci run name."},
    "ci_unknown": {"es": "No hay ningún {what} llamado {name}.", "en": "There is no {what} called {name}."},
    "fix.ci_unknown": {"es": "Usa el nombre de uno de los declarados en el archivo.", "en": "Use the name of one declared in the file."},
    "ci_secret": {"es": "Faltan los secretos {names} del pipeline {pipeline}.", "en": "The secrets {names} of the pipeline {pipeline} are missing."},
    "fix.ci_secret": {"es": "Defínelos como variables de entorno antes de ejecutarlo.", "en": "Define them as environment variables before running it."},
    "ci_target": {"es": "\"{target}\" no es un servicio de CI que nexo ci export conozca.", "en": "\"{target}\" is not a CI service that nexo ci export knows."},
    "fix.ci_target": {"es": "Usa github, gitlab, azure o jenkins.", "en": "Use github, gitlab, azure or jenkins."},
    "err.ci_run": {"es": "El comando \"{command}\" ha terminado con el código {code}", "en": "The command \"{command}\" ended with code {code}"},
    "fix.ci_run": {"es": "Mira su salida, arriba, para saber qué ha fallado.", "en": "Look at its output, above, to see what failed."},
    "err.ci_tests": {"es": "Hay tests que fallan", "en": "Some tests fail"},
    "fix.ci_tests": {"es": "Mira el informe de nexo test, arriba, y arréglalos.", "en": "Look at the report of nexo test, above, and fix them."},
    "err.ci_apply": {"es": "nexo apply no ha terminado bien", "en": "nexo apply did not end well"},
    "fix.ci_apply": {"es": "Mira su salida, arriba, para saber qué ha fallado.", "en": "Look at its output, above, to see what failed."},
    "err.ci_approve": {"es": "El paso {step} no se ha aprobado", "en": "The step {step} was not approved"},
    "fix.ci_approve": {"es": "Vuelve a ejecutarlo y responde sí cuando lo pida.", "en": "Run it again and answer yes when it asks."},
    "info.ci_step": {"es": "== paso {step}", "en": "== step {step}"},
    "info.ci_after": {"es": "== after {what}", "en": "== after {what}"},
    "info.ci_failed": {"es": "Pipeline {pipeline}: el paso {step} ha fallado.", "en": "Pipeline {pipeline}: the step {step} failed."},
    "info.ci_done": {"es": "Pipeline {pipeline}: {n} pasos correctos, versión {version}.", "en": "Pipeline {pipeline}: {n} steps passed, version {version}."},
    "info.ci_approve": {"es": "¿Aprobar el paso {step}? Lo aprueban {names}. Escribe sí para seguir: ", "en": "Approve the step {step}? {names} approve it. Type yes to go on: "},
    "info.ci_approved": {"es": "Paso {step} aprobado con --yes (lo aprueban {names}).", "en": "Step {step} approved with --yes ({names} approve it)."},
    "info.ci_comment": {"es": "Comentario para la revisión:", "en": "Comment for the review:"},
    "info.ci_exported": {"es": "Escrito {path}.", "en": "Wrote {path}."},
    "info.drift_none": {"es": "Ninguno: la nube coincide con el estado.", "en": "None: the cloud matches the state."},
    "info.drift_found": {"es": "Diferencias: {n}. nexo apply las devuelve a lo que dice el programa.", "en": "Differences: {n}. nexo apply brings them back to what the program says."},
    "info.imported": {"es": "Importado: {what} ({id}).", "en": "Imported: {what} ({id})."},
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
