# Nexo Specification

Edition 0.1 · October 23, 2026

Nexo 0.1 fixes a single decision for every open point in the design: wherever there used to be two ways to do something, there is now one. File extension: `.nexo`. Command: `nexo`.

## Goals and guarantees

Nexo aims to be the safest, smartest and most effective language to use, and each of those words translates into guarantees that can be checked. Being "the most" is proven with public evidence, not with design: that is why every guarantee states how it is verified.

**Safer**

| Guarantee | How it is met | How it is verified |
| --- | --- | --- |
| No memory errors or data races | memory model in section 7 and concurrency in section 5; only `unsafe` can bypass the rules | public test suite and continuous fuzzing of the compiler |
| `unsafe` under control | forbidden in published packages unless there is a signed review; `nexo audit` lists every use | public registry of packages with reviewed `unsafe` |
| Explicit API permissions | the program `policy` limits checked Nexo APIs; optional Shield ceilings attenuate dependency and dispatch authority. Policy alone does not prevent DNS rebinding, mediate direct native syscalls or impose process resource limits | `nexo policy show` and policy-boundary regression tests; OS isolation requires a separate supported profile |
| Hardened supply chain | reproducible builds, signed packages and an SBOM generated on every `nexo build --release` | two builds of the same code produce the same binary byte for byte |
| Verifiable contracts | `expect`, `ensure` and `old()` in functions (see below) | the compiler proves what it can and checks the rest at runtime |
| Cryptography without dangerous choices | algorithms fixed per edition (section 9) | external audit of `crypto` before each edition |

```nexo
fn transfer(source: Account, target: Account, amount: float):
    expect amount > 0
    expect source.balance >= amount
    ensure source.balance + target.balance == old(source.balance + target.balance)

    source.balance -= amount
    target.balance += amount
```

`expect` states what must hold when the function is entered and `ensure` what must hold when it returns; `old()` gives the value an expression had on entry. A contract the compiler can prove costs nothing at runtime. One it cannot prove is checked at runtime and, if it fails, raises a `ContractBroken` error with the values involved. `nexo build --release` keeps every `expect` active; unproven `ensure` checks can be disabled with `--trust-ensure`.

`expect` and `ensure` become reserved words of the core (39 in total). `expect` is used instead of `require` because `require` already means a `policy` requirement.

**Smarter**

| Guarantee | How it is met | How it is verified |
| --- | --- | --- |
| The compiler understands intent | `nexo check --intent` compares `##` comments and contracts with what the code does and warns when they disagree | public set of cases with known mismatches |
| Tests proposed automatically | `nexo test --suggest` generates edge cases from types, contracts and unions | branch coverage before and after |
| Every error comes with its fix | every message includes a correction; `nexo fix` applies the safe ones | percentage of errors with an applicable fix, published with each release |
| Verifiable AI-generated code | one way to do each thing, types at boundaries and contracts; `nexo check` validates generated code before it runs | rate of LLM-generated programs that compile and pass their tests on the first try |
| AI does not invent APIs | `nexo docs --for-ai` gives any model the exact list of available functions | public comparison with and without that summary |

**More effective**

| Goal | Value | How it is verified |
| --- | --- | --- |
| Performance on typical code | under 1.5× Rust's run time | public, reproducible benchmarks against Rust, Go and Python |
| Memory | under 1.3× Rust's RAM | same benchmarks |
| Compilation | 100,000 lines in under 10 s in development mode | measured on every release |
| Program start-up | under 20 ms | measured on every release |
| First useful program | under 1 hour for someone with no experience | studies with real users |
| From idea to production | code, infrastructure and deployment in a single project (sections 11 and 12) | time measured on the official examples |

**Transparency commitment.** Before edition 1.0 the following are published: the benchmarks and their code, an external security audit of the compiler and of `crypto`, and the results of the user studies. A guarantee that is not met in testing is either fixed or removed from this document.

## 1. Principles: core versus modules

Nexo has a small, stable core; everything else is an official module loaded with `use`. This keeps the language easy even though it covers AI, web, infrastructure and CI.

Five rules settle any design question:

1. One way to do each thing.
2. What is safe and efficient is what you write by default; anything costly or dangerous is always visible in the code.
3. If someone with no experience cannot understand it by reading it, it does not go into the core.
4. No keyword has two meanings.
5. Whatever does not fit in the core lives in an official module with the same syntax.

| Layer | Contents | How it is used |
| --- | --- | --- |
| Core | variables, types, functions, control flow, errors, collections, text, pipelines, `parallel` | always available |
| Standard library | files, network, JSON/CSV, dates, units, tables, encryption, tests | always available, no `use` |
| Official modules | `nexo.ai`, `nexo.web`, `nexo.db`, `nexo.infra`, `nexo.ci`, `nexo.ui` | `use nexo.ai` |
| Third-party packages | any published library | `nexo add name` + `use name` |

To keep examples short, a file that uses `ask`, `server`, `infra` or `pipeline` loads its module automatically; `nexo fmt` adds the explicit `use` on save.

## 2. Basic syntax

Every construct has a single form. These are the decisions that close earlier inconsistencies:

| Topic | Decision | Removed |
| --- | --- | --- |
| Variables | `x = 1` immutable, `var x = 1` mutable | `let` |
| Types | optional and inferred; required only on `pub` functions | mandatory types everywhere |
| Missing values | `T?` and the `??` operator for a default | unchecked `null` |
| User-defined data | `type` for records, unions and enums | `struct`, `enum`, `impl` |
| Calls | always with parentheses: `print(x)`, `ask("...")` | `print x` without parentheses |
| Return | `return` is always explicit | implicit return of the last line |
| Blocks | colon and 4-space indentation | braces |
| Comments | `#`, and `##` for documentation | `//`, `/* */` |

**Variables, text and collections**

```nexo
name = "Ana"
var counter = 0
age: int = 30                      # optional type
email: str? = none                 # may be missing

print("Hello {name}, you are {age}")
print(email ?? "no email")

fruits = ["pear", "grape"]         # list
prices = {"pear": 1.2, "grape": 3} # map
tags = set("new", "sale")          # set
text = """
    Several lines
    with {name}
"""
```

**User-defined types: one word, `type`**

```nexo
type User:
    name: str
    age: int
    email: str?

    fn is_adult(self) -> bool:
        return self.age >= 18

type Status = "active" | "closed"   # union of values

type Event:                          # enum with data
    Click(x: int, y: int)
    Key(code: str)
    Quit

trait Shape:
    fn area(self) -> float

type Circle is Shape:
    radius: float
    fn area(self) -> float:
        return PI * self.radius ** 2
```

**Functions**

```nexo
fn greet(person, greeting = "Hello"):
    return "{greeting}, {person}"

pub fn total(prices: list[float]) -> float:   # types required because it is pub
    return prices.sum()

double = x -> x * 2                            # lambda: a single expression
```

**Control flow**

```nexo
if age >= 18:
    print("Adult")
elif age >= 13:
    print("Teenager")
else:
    print("Child")

for fruit in fruits:
    print(fruit)

for i in 1..10:          # inclusive range
    print(i)

repeat 3:
    print("Hello")

while counter < 5:
    counter += 1

match event:
    Click(x, y) if x > 100: print("right")
    Key("q") or Quit:       close()
    _:                      pass
```

`match` uses a colon like the rest of the language; the `=>` arrow is gone. Comprehensions such as `[n * 2 for n in list if n > 0]` remain the only short way to transform collections outside a pipeline.

## 3. Errors

An error is a value, never a hidden exception, and there are only two things you can do with it: pass it up with `?` or handle it with `try`. `match ok/err`, `or Error` and `try/else` without a variable are gone.

**Rules**

1. A function that can fail returns `T!` ("T or an error"). The compiler infers it; on `pub` functions it is written out.
2. Ignoring a possible error is a compile error.
3. `?` passes the error to the caller. It can only be used inside a function that also returns `!`.
4. `try` + `else e` handles the error on the spot.
5. `fail(...)` creates a custom error. System errors carry a message, a cause and a line.
6. A program that ends with an unhandled error shows it in plain language and exits with code 1.

```nexo
pub fn load_config(path: str) -> Config!:
    text = read(path)?                # if it fails, return the error
    return json.parse(text, as: Config)?

try:
    cfg = load_config("app.json")
    print("Port: {cfg.port}")
else e:
    print("Could not load the configuration: {e.message}")

fn divide(a, b):
    if b == 0:
        fail("Cannot divide by zero")
    return a / b

try:
    data = read("config.txt")
else e is NotFound:                   # handle a specific type
    data = ""
else e:
    log.error(e)
```

Missing values and errors never mix: `??` is only for `T?` values that may be missing; `?` and `try` are only for `T!` values that may fail.

## 4. Events and blocks

Every event word has a single meaning. `on` is reserved for the application's own events; everything else has its own word.

| Word | Single meaning | Example | Before |
| --- | --- | --- | --- |
| `on` | react to an event raised with `emit` | `on order_created(o):` | also clouds, CI and shutdown |
| `when` | react to something external (email, file, webhook, queue) | `when email.new(inbox: "support") as mail:` | same |
| `every` | scheduled task | `every day at 09:00:` | same |
| `at` | a point in time, only with `every` and `scale` | `scale at "daily 23:00", min: 2` | also a cloud target |
| `cloud =` | infrastructure target, as a key of the `infra` block | `cloud = "aws"` | `infra web on aws` |
| `trigger` | what starts a CI pipeline | `trigger push to main` | `on push to main` |
| `after` | pipeline outcome hook | `after success:` / `after failure:` | `on success` |
| `lifecycle` | program start-up and shutdown | `lifecycle stop, max 30s:` | `on shutdown` |

```nexo
on order_created(order):
    send_email(order.customer, "Thank you for your purchase!")

emit order_created(order)

when file.new("inbox/") as incoming:
    process(incoming)

every monday at 08:00:
    build_report()

lifecycle stop, max 30s:
    db.close()
```

**Configuration blocks.** Every block that configures something follows the pattern `word name?:` followed by `key = value` pairs. A colon between key and value is only used in types and named arguments (`age: int`, `model: "fast"`). This rule applies to `ai`, `policy`, `infra`, `app`, `pipeline`, `server`, `stage` and `env` (environment variables).

## 5. Concurrency

Nexo has no `async/await`: every network, disk or AI operation is non-blocking under the hood, as in Go, and code is written in a straight line. There are two levels, and the basic one is enough for almost everything.

| Level | Words | Purpose |
| --- | --- | --- |
| Basic (core) | `parallel for`, `parallel:` | do several things at once and wait for all the results |
| Advanced (`nexo.sync`) | `spawn`, `channel`, `select`, `shared` | long-running workers, queues and shared state |

Removed: `await`, `await all`, `ask stream` as a separate word, and `share` (now `shared`).

**Basic**

```nexo
# each element in parallel; returns the list of results in order
pages = parallel for url in urls:
    return http.get(url)

# several different tasks at once; finishes when all of them finish
parallel:
    sales = read("sales.csv")
    customers = db.customers.all()
    exchange_rate = http.get(EXCHANGE_API)

# optional limit on simultaneous tasks
parallel for img in images, max: 8:
    compress(img)
```

If one task in a `parallel` fails, the others are cancelled and the error propagates like any `T!`.

**Advanced**

```nexo
use nexo.sync

queue = channel[Order](size: 100)

spawn producer(queue)
repeat 8:
    spawn worker(queue)

fn worker(c: channel[Order]):
    for o in c:
        process(o)

select:
    msg from input:     process(msg)
    err from errors:    log.error(err)
    after 5s:           print("idle")

shared cache = {}          # the compiler adds the necessary locking
```

Guarantee: the compiler rejects any mutable data shared between tasks that is not `shared` and does not travel through a `channel`. There are no data races in code without `unsafe`.

## 6. Data, pipelines and databases

The `|>` operator has one fixed rule: it passes the value on the left as the first argument of the step on the right. The same words work for lists, tables, files and databases.

**Pipeline rules**

1. With tables and databases, column names are written as they are: `where amount > 1000`.
2. With lists of plain values, the current element is called `it`: `where it > 0`.
3. Valid steps: `where`, `map`, `pick`, `group by`, `sum`, `count`, `avg`, `sort by`, `take`, `each`, `save`, and any function.
4. Every pipeline is lazy: it reads nothing until a final step (`save`, `each`, `show`, an assignment) needs it.

Removed: `filter row.amount`, and `sum amount` without a preceding grouping word.

```nexo
read("sales.parquet")
  |> where amount > 1000
  |> group by region
  |> sum amount
  |> sort by amount desc
  |> save("summary.csv")

positives = numbers |> where it > 0 |> map it * 2

for line in read_lines("log_50GB.txt") |> where "ERROR" in it |> take 100:
    print(line)
```

**Databases (`nexo.db`)**

There is one way to connect, whether to a local file or a server. The word `store` is gone: a local file is simply `connect("data.db")`.

```nexo
use nexo.db

db = connect("postgres://prod/shop", key: secret("DB_PASS"))
local = connect("data.db")               # embedded SQLite

@table
type Customer:
    id: id                               # automatic primary key
    name: str
    email: str @unique
    orders: many Order                   # one-to-many relation

@table
type Order:
    id: id
    customer: ref Customer               # foreign key
    amount: float
    date: date = today

# queries use the same pipeline; they are translated to SQL
large = db.Order |> where amount > 100 |> sort by date desc |> take 20
ana = db.Customer.find(email: "ana@mail.com")?
print(ana.orders.count())

# writes
db.Order.add(customer: ana, amount: 49.9)
db.transaction:
    db.Order.add(customer: ana, amount: 10)
    db.Customer.update(ana, name: "Ana G.")

# raw SQL, always parameterised
rows = db.sql("SELECT * FROM order WHERE amount > {minimum}")
```

**Migrations.** `@table` types are the source of truth. `nexo db diff` compares the types with the real database and generates a readable migration file; `nexo db migrate` applies it inside a transaction. A migration that drops columns or tables requires `--allow-drop`.

## 7. Memory and performance

Nexo compiles to native code with LLVM and has no garbage collector. A known cost is accepted: wherever the compiler cannot prove a single owner, it uses reference counting. The measurable goal is to stay under 1.5× Rust's run time on typical code, without the user writing memory annotations.

**Memory model (in this order)**

1. Small values (numbers, `type` with no collections inside) are copied and live on the stack.
2. Collections and text are borrowed by default; if they are modified after being shared, they are copied at that moment (copy-on-write).
3. If the compiler proves a single owner, it moves the value at no cost and frees it when it goes out of scope.
4. If it cannot prove that, it uses reference counting. A reference cycle between `type` values is a compile error unless a field is marked `weak`.

Removed from the core: `view`, `move`, `copy`, `arena`, `@packed`, `@memory_limit` and mandatory fixed-size types. All of them move to `nexo.perf`, only for those who need them.

```nexo
type Node:
    value: int
    children: list[Node]
    parent: weak Node?       # breaks the parent-child cycle
```

**Automatic optimisations**

- Fusion of pipelines and comprehensions into a single loop.
- SIMD vectorisation of simple numeric loops.
- Parallelisation of table operations above 100,000 rows.
- Chunked reading of large files and columnar tables.
- `int` and `float` are 64-bit; the compiler narrows them inside tables and lists when the range allows it.

**Manual control (`nexo.perf`)**

```nexo
use nexo.perf

@inline
fn square(x: float) -> float:
    return x * x

type Pixel @packed:
    r: u8
    g: u8
    b: u8

arena tmp:
    for row in csv:
        node = tmp.new(Node(row))

data = mmap[f32]("sensors.bin")

unsafe:
    ptr.raw(buffer).write(0, 255)
```

**Compilation.** `nexo app.nexo` compiles in fast development mode and runs. `nexo build --release` applies full optimisation. `nexo profile app.nexo` shows in plain language which lines use the most CPU and RAM, and where reference counting or data copies happen.

## 8. AI (`nexo.ai`)

`ask` is an ordinary function: `ask(instruction, input?, options...)`. It returns text or, when assigned to a typed variable, a validated value of that type. A tool is any `fn` marked with `@tool`, and an agent is declared with the `agent` block.

**Configuration precedence (the first one that exists wins)**

1. Options on the call itself: `ask(..., model: fast)`.
2. Profile named on the call: `ask(..., profile: "work")`.
3. The file's `ai` block.
4. The `ai` block in the project's `nexo.toml`.
5. User configuration saved with `nexo ai setup`.

If none exists, `ask` fails with the message "No AI provider is configured. Run: nexo ai setup".

```nexo
ai:
    provider = "anthropic"
    model = smart
    key = secret("ANTHROPIC_KEY")
    fallback = [fast, local]
    budget = 5 EUR per day
    when_over_budget = local

ai models:
    fast  = "openai:gpt-mini"
    smart = "anthropic:claude-opus"
    local = "ollama:llama"

ai profile "work":
    provider = "anthropic"
    key = secret("COMPANY_KEY")

# calls
summary = ask("Summarise this text", text)
label = ask("Classify", mail, model: fast, creativity: low, timeout: 30s)

type Opinion:
    tone: "positive" | "negative" | "neutral"
    score: 1..5

o: Opinion = ask("Analyse this review", review)   # validated, up to 3 retries

for chunk in ask("Tell me a story", stream: true):
    print(chunk, end: "")

r = ask("Summarise the email", mail, guard: injection, redact: private)
```

**`ask` options**

| Option | Values | Default |
| --- | --- | --- |
| `model` | alias or `"provider:model"` | the configured one |
| `profile` | profile name | none |
| `creativity` | `low`, `medium`, `high` | `medium` |
| `max` | number + `words` or `tokens` | no limit |
| `timeout` | duration | `60s` |
| `retries` | number | `3` with a type, `1` without |
| `stream` | `true`, `false` | `false` |
| `guard` | `injection`, `none` | `injection` when the input is external |
| `redact` | `private`, `none` | `private` |

**Agents and tools**

```nexo
@tool "Looks up an order by its number"
fn find_order(id: str) -> Order!:
    return db.Order.find(id: id)?

agent support:
    model = fast
    tools = [find_order, send_email]
    goal = "Answer order questions politely"
    max_steps = 10
    confirm = [send_email]           # asks for human approval before using it

answer = support.handle(mail)
```

An agent's tools inherit the program's `policy`, and any MCP server is connected with `tools = [mcp("name")]`. `nexo ai usage` shows spend per model and per day; keys never appear in logs or traces.

## 9. Security

A program compiled with `nexo build --release` cannot do anything its `policy` block does not allow; without a `policy`, the production build fails. In development mode (`nexo app.nexo`) everything is allowed except `shell`, and every undeclared access is reported so that copying it into the policy is trivial.

**Policies**

```nexo
policy:
    allow read  "data/"
    allow write "output/"
    allow net   "api.company.com", "*.openai.com"
    allow env   "PORT"
    deny  shell
```

Anything not listed is forbidden. Dependencies inherit the policy: a library cannot open network or disk access the program has not allowed. `nexo policy suggest` generates the minimal policy from a test run.

**Secrets and rotation**

```nexo
db_pass = secret("DB_PASS")                    # system keychain, then environment variable
api_key = secret("API_KEY", from: vault)       # also: aws, azure, gcp

rotate secret("DB_PASS") every 30 days:
    new_pass = password.generate(length: 32)
    db.change_password(new_pass)
    notify("security@company.com", "DB_PASS rotated")
```

A `secret` value cannot be printed, converted to text, written to a file or sent to a destination the policy does not allow: the compiler rejects it. Writing a literal key in the code is a compile error.

**Passwords, encryption and signatures**

```nexo
stored = password.hash(entered)                # Argon2id
if password.check(entered, stored):
    login(user)

k = key.new()                                  # AES-256-GCM
hidden = encrypt(data, k)
data = decrypt(hidden, k)?
encrypt_file("customers.csv", k)

pair = keypair.new()                           # Ed25519
signature = sign(document, pair.private)
if verify(document, signature, pair.public):
    print("Authentic")
```

Algorithms are fixed per language edition; none is chosen by hand. If an algorithm stops being secure, the next edition replaces it and `nexo migrate` re-encrypts the data.

**External data**

Every value that comes from outside (`input`, web requests, files outside the project, `ask` responses) is marked as external. An external value cannot reach `shell`, a file path or HTML without going through `validate`. In SQL and web templates, interpolation is escaped automatically.

```nexo
email = validate(input("Email"), as: email)?
age = validate(form.age, as: int, range: 0..120)?
```

**Personal data and auditing**

```nexo
type Customer:
    name: str
    national_id: str @private
    card: str @private

log(customer)           # {name: "Ana", national_id: "***", card: "***"}
audit("user deleted", by: user, target: id)
```

`@private` fields are hidden in logs, traces and AI calls. `audit` writes to a signed, append-only log.

**Tools**

| Command | What it does |
| --- | --- |
| `nexo audit` | checks dependencies for known vulnerabilities |
| `nexo scan` | looks for secrets in the code and bad practices |
| `nexo policy show` | shows what the program can actually do |
| `nexo policy suggest` | proposes the minimal policy |

## 10. Design patterns

A pattern only gets its own word if it saves real code and real errors; the rest are expressed with what already exists. This keeps the one-way rule.

| Pattern | Form in Nexo | Decision |
| --- | --- | --- |
| Singleton | `single name:` | own word |
| Observer | `on` + `emit` | own word |
| State machine | `states name:` | own word |
| Command with undo | `action name:` + `undo()` | own word |
| Circuit breaker | `protect` | own word |
| Saga / durable workflow | `workflow name:` | own word |
| Decorator | `@cache`, `@retry`, `@limit`, `@tool` | annotation |
| Strategy | map of functions or `match` | no word; `choose` removed |
| Factory | function with `match` | no word; `make` removed |
| Builder | named arguments with defaults | no word; `with` removed |
| Adapter | `convert(x, to: format)` | standard library function |
| Repository | `@table` types from `nexo.db` | `store` removed |
| Pipeline | pipe operator (section 6) | core operator |

```nexo
single config:
    port = 8080
    mode = "production"

states order:
    new -> paid -> shipped -> delivered
    new -> cancelled
    paid -> cancelled

order.go("paid")
order.go("delivered")      # error: it must be "shipped" first

action trash(path):
    do:   move_file(path, to: "trash/")
    undo: move_file("trash/{path.name}", to: path.folder)

trash("photo.jpg")
undo()

protect(payments_api, fails: 5, wait: 1m):
    charge(order)

@cache(10m)
@retry(3, backoff: exp)
fn dollar_price():
    return http.get(EXCHANGE_API).json().usd

# Strategy and Factory: ordinary functions
rates = {"ES": domestic_rate, "FR": europe_rate}
cost = (rates[country] ?? world_rate)(parcel)

# Builder: named arguments
house = House(bedrooms: 3, pool: true, garage: 2)
```

**Durable workflows.** A `workflow` saves its state after every step and resumes after a restart or a failure. If a step fails permanently, the `undo` of every completed step runs in reverse order.

```nexo
workflow customer_signup(email):
    step create_account(email)
        undo delete_account(email)
    step wait email.confirmed(email), max: 3 days
    step send_welcome(email)
```

## 11. Infrastructure (`nexo.infra`)

`infra name:` declares resources and its `cloud` key sets the target: `"aws"`, `"azure"`, `"gcp"` or `"k8s"`. `nexo plan` computes the changes and `nexo apply` applies them. Portable resources cover what is common to the four targets; everything else is written with `native`, which is the only way to use a provider-specific resource.

**Portable resources**

| Resource | AWS | Azure | GCP | k8s |
| --- | --- | --- | --- | --- |
| `app` | ECS Fargate | Container Apps | Cloud Run | Deployment + Service + HPA |
| `server` | EC2 | Virtual Machine | Compute Engine | not applicable |
| `database` | RDS | Azure Database | Cloud SQL | StatefulSet (dev only) |
| `storage` | S3 | Blob Storage | Cloud Storage | PersistentVolume |
| `cache` | ElastiCache | Azure Cache for Redis | Memorystore | operator-managed Redis |
| `queue` | SQS | Service Bus | Pub/Sub | queue operator |
| `cluster` | EKS | AKS | GKE | not applicable |
| `network` | VPC | VNet | VPC | NetworkPolicy |
| `dns` | Route 53 | Azure DNS | Cloud DNS | Ingress + cert-manager |
| `identity` | IAM Role | Managed Identity | Service Account | ServiceAccount + RBAC |

A portable resource used on a target with no equivalent is a `nexo plan` error, never silent behaviour.

```nexo
infra shop:
    cloud = "aws"
    region = "eu-west-1"
    state = s3("nexo-state-shop")          # encrypted and locked
    budget = 300 EUR per month

    policy:
        deny public database
        require encryption
        require tags ["team", "cost"]

    network net:
        range = "10.0.0.0/16"
        private = true
        allow from internet to api, port: 443

    identity api_id:
        can read storage photos
        can write queue orders

    database customers:
        type = "postgres"
        size = medium
        network = net

    storage photos:
        public = false

    queue orders

    app api:
        image = "shop-api:2.4"
        identity = api_id
        env:
            DB = customers.url
            API_KEY = secret("API_KEY")

    dns "api.shop.com" -> api, https

    native "aws_wafv2_web_acl" firewall:
        scope = "REGIONAL"

    output url = api.url
    output db = customers.url, secret
```

**Kubernetes and clusters**

```nexo
infra platform:
    cloud = "gcp"

    cluster k:
        nodes = 3..6

    app api:
        cluster = k                # deploy inside the cluster created above
        image = "shop-api:2.4"
        cpu = 0.5
        ram = 512 MB
        port = 8080
        health = "/health"

infra existing:
    cloud = "k8s"
    cluster = "production"         # existing cluster
    namespace = "shop"
```

Several clouds at once are declared as several `infra` blocks, one per target; `nexo apply` applies them all and respects the references between them.

**Stages.** A `stage` block overrides values for one stage; `nexo apply --stage prod` chooses which. A value without a stage applies to all of them. `stage` is used instead of `env` because `env` always means environment variables.

```nexo
stage dev:
    size = small
    replicas = 1

stage prod:
    size = large
    replicas = 3..10
```

**Commands**

| Command | What it does |
| --- | --- |
| `nexo plan` | shows what will be created, changed or deleted, the estimated cost and the estimated availability |
| `nexo apply` | applies the plan after confirmation; in CI it requires `--yes` |
| `nexo destroy` | deletes everything; requires typing the infra name |
| `nexo drift` | detects changes made outside Nexo |
| `nexo import aws app i-0abc123` | adopts an existing resource |
| `nexo cost` | estimates the monthly cost |

## 12. CI/CD, deployment, resilience and scaling

One file describes the application, its infrastructure and how it reaches production. The steps of a `pipeline` are ordinary Nexo code; external commands are launched with `run(...)` and are subject to the `policy`.

**Pipeline (`nexo.ci`)**

```nexo
pipeline main:
    trigger push to main
    secrets = ["AWS_KEY", "DB_PASS"] from vault
    version = auto                 # fix -> patch, feat -> minor, breaking -> major

    step checks:
        parallel:
            test()
            audit()
            scan()

    step build:
        image = build_image("shop-api")

    step dev:
        apply(stage: "dev", image: image)

    step prod:
        approve(by: ["ana@company.com", "luis@company.com"])
        apply(stage: "prod", image: image)

    after success:
        slack("#deploys", "v{version} is in production")
    after failure:
        slack("#deploys", "Step {step} failed")

pipeline review:
    trigger pull_request
    step plan:
        comment(plan(stage: "prod"))      # posts the plan on the PR
```

Steps run in the order written; `parallel` inside a step is the only way to run things in parallel. `nexo ci export github` (also `gitlab`, `azure`, `jenkins`) generates the native file, and `nexo ci run` runs the pipeline locally.

**Progressive delivery**

Each `app` declares a single strategy with `release`. Valid values: `canary`, `blue_green`, `rolling`. Default: `rolling`.

```nexo
app api:
    image = "shop-api:2.4"

    release canary:
        steps = [5%, 25%, 50%, 100%]
        every = 10m
        watch = [errors < 1%, latency.p95 < 300ms]
        rollback = auto
        audience = users where country == "ES"    # optional: canary by user
```

At each stage Nexo compares the `watch` metrics with the previous version; if any fails, it rolls back. `nexo release status`, `promote` and `abort` control the process by hand.

**High availability**

```nexo
app api:
    spread = zones                 # replicas spread across zones
    min_available = 2              # also during deployments
    regions = ["eu-west", "us-east"]
    traffic = nearest
    failover = auto
    health = "/health"

database customers:
    replicas = 2
    failover = auto
    backup = every 6h, keep 30 days
    restore_test = weekly
```

**Resilience**

Calls to other services are configured in a `calls` block. The order of application is fixed: `timeout`, then `retry`, then `protect` (circuit breaker) and, if everything fails, `fallback`.

```nexo
app api:
    calls payments:
        timeout = 2s
        retry = 3, backoff: exp
        protect = fails 5, wait 30s
        isolate = 20                   # maximum simultaneous calls (bulkhead)
        fallback = payments_queue.add

fn recommendations(user):
    try within 300ms:
        return model(user)
    else e:
        return popular()               # graceful degradation
```

`nexo chaos api --kill 1`, `--latency 500ms payments` and `--region eu-west down` inject failures and return a report of what held up.

**Scaling**

```nexo
app api:
    scale 3..50:
        cpu > 70%
        requests > 1000 per replica
        queue orders > 500
    scale at "Mon-Fri 08:00", min: 10
    scale at "daily 23:00", min: 2

app reports:
    scale 0..5                         # down to zero with no traffic

database customers:
    size = auto, max: large
    read_replicas = 1..5
```

It scales up as soon as any condition is met, and down only when none has been met for 5 minutes.

## 13. Packages, versions and evolution

Every project has a `nexo.toml` and a `nexo.lock`. Versions follow SemVer, resolution is deterministic (always the same version for the same lock file) and there can only be one version of each package per program.

**Project**

```toml
[project]
name = "shop"
version = "1.4.0"
nexo = "0.1"            # language edition

[dependencies]
stripe-payments = "^2.1"

[python]
numpy = "^2.0"          # Python interoperability

[rust]
serde_json = "^1.0"     # Rust interoperability
```

```nexo
use stripe_payments
use python numpy as np
use rust serde_json
```

**Package rules**

1. `nexo add name` adds the latest compatible version and updates the lock file.
2. Every published package is signed; a package without a valid signature is not installed.
3. `nexo audit` runs on install; a critical vulnerability blocks the install unless `--accept-risk` is given.
4. A package declares the permissions it needs; if they exceed the project's `policy`, `nexo add` rejects it.
5. Two dependencies requiring incompatible versions of the same package are an error with an explanation, never two copies.
6. Visibility: only what is marked `pub` is exported. A module is a file; its name is its path (`shop/payments.nexo` is `shop.payments`).

**Language evolution: editions**

1. The language moves forward in editions (`0.1`, `1.0`, `2.0`). Each project pins its edition in `nexo.toml`.
2. A new compiler compiles all previous editions; projects on different editions can use each other.
3. An incompatible change only lands in a new edition, never in a minor update.
4. `nexo migrate` rewrites code from one edition to the next automatically and shows the diff before saving.
5. A deprecated function warns for a full edition before it disappears in the next one.

## 14. Tooling

Everything is installed as a single binary, `nexo`; there are no separate tools to choose. The formatter has no options, so all Nexo code looks the same.

| Command | What it does |
| --- | --- |
| `nexo` | opens the interactive REPL to try code line by line |
| `nexo app.nexo` | compiles in development mode and runs |
| `nexo new name` | creates a project with `nexo.toml`, a `src/` folder and a test |
| `nexo build --release` | optimised build; `--target web`, `mobile`, `linux-arm`, `arduino` |
| `nexo test` | runs the `test` blocks |
| `nexo fmt` | formats and adds implicit `use` lines |
| `nexo check [paths...]` | checks without executing the program |
| `nexo check --effects [--json] [--require-complete] [--dependencies file.json] [--authorization file.json] [paths...]` | conservative effect requirements and configured Shield ceiling comparison; see [the model and limits](effects.md) (D182–D183) |
| `nexo check --json [paths...]` | versioned structured diagnostics; see [the v1 contract](diagnostics.md) (D181) |
| `nexo lint` | warns about questionable code with a proposed fix |
| `nexo fix` | applies the linter's safe fixes |
| `nexo profile` | CPU and RAM usage per line |
| `nexo debug --replay` | records the run and lets you step backwards |
| `nexo docs` | generates documentation from `##` comments |
| `nexo docs --for-ai` | compact project summary so an LLM can program against it |
| `nexo lsp` | language server for any editor |

**Tests**

```nexo
test "basic sum":
    assert add(2, 3) == 5

test "fails with an invalid path":
    assert load_config("missing.json") fails

test "charge":
    fake payments_api returns {ok: true}
    fake ask returns "positive"           # AI calls can be faked too
    assert charge(order).ok
```

**Observability.** Every program emits traces and metrics in OpenTelemetry format with no configuration: every `pub` function, every `ask` call, every web request and every database query. `@metric` adds a custom metric; `log(...)` writes structured logs with `@private` fields already hidden.

**Error messages.** They all follow the same format: file and line, the highlighted snippet, one sentence on what went wrong and one on how to fix it. Messages are shown in the system language (see section 16).

```
Error in app.nexo, line 4:
  print(nmae)
        ^^^^
"nmae" does not exist. Did you mean "name"?
```

## 15. Standard library and official modules

The standard library is always available without `use`; official modules are loaded with `use` and are maintained by the same team as the language.

| Name | Main contents | Loading |
| --- | --- | --- |
| `io` | `print`, `input`, `read`, `write`, `read_lines`, `move_file` | always |
| `text` | interpolation, search, regular expressions, templates | always |
| `data` | `convert`, JSON, CSV, Parquet, XML, YAML, columnar tables | always |
| `time` | `now`, `today`, durations (`5m`, `2 days`), time zones | always |
| `units` | `km`, `MB`, `EUR`, automatic unit conversion | always |
| `http` | `http.get`, `http.post`, client with retries | always |
| `crypto` | `secret`, `password`, `key`, `encrypt`, `sign`, `validate` | always |
| `test` | `test`, `assert`, `fake` | always |
| `nexo.ai` | `ask`, `agent`, `@tool`, `ai` configuration | implicit `use` |
| `nexo.web` | `server`, routes, authentication, `@limit` | implicit `use` |
| `nexo.db` | `connect`, `@table`, migrations | `use nexo.db` |
| `nexo.sync` | `spawn`, `channel`, `select`, `shared` | `use nexo.sync` |
| `nexo.perf` | `@inline`, `@packed`, `arena`, `mmap`, `unsafe`, types `u8` to `f64` | `use nexo.perf` |
| `nexo.infra` | `infra`, `app`, `database`, `stage` | implicit `use` |
| `nexo.ci` | `pipeline`, `step`, `trigger`, `release` | implicit `use` |
| `nexo.ui` | `window`, `button`, graphical `input` | `use nexo.ui` |
| `nexo.notify` | `send_email`, `slack`, `notify` | `use nexo.notify` |

**Web server**

```nexo
server api:
    port = 443
    auth = jwt(key: secret("JWT_KEY"))

    get "/users/{id}"(id: int):
        return db.User.find(id: id) ?? 404

    @role("admin")
    @limit(10 per minute per ip)
    delete "/users/{id}"(id: int):
        db.User.remove(id)
        return 204
```

HTTPS, secure headers and CSRF protection are on by default and cannot be turned off without `unsafe`.

## 16. Learning and languages

Canonical keywords are in English and are the only ones saved in files. Human languages are a display layer: the editor can show and accept keywords in Spanish, but the saved file is always the same, so any person or LLM reads the same code.

**Rules**

1. Every keyword has one official translation per language (`if` / `si`, `for` / `para`, `return` / `devuelve`).
2. `nexo fmt --view es` shows the file in Spanish; saving writes the canonical form.
3. User identifiers (variables, functions, types) can be written in any language and accept accents and ñ.
4. Error messages, help and `nexo learn` use the system language; `nexo config lang es` sets it.

```nexo
# Spanish view (editor)                # saved file
si edad >= 18:                         # if edad >= 18:
    imprimir("Adulto")                 #     print("Adulto")
```

**Learning**

- `nexo learn` opens an interactive course in the terminal: 20 short lessons, from variables to deploying to the cloud.
- `nexo explain file.nexo` describes in plain language what a program does, line by line.
- `nexo examples` lists complete programs ready to copy: email bot, API, daily report, deployment.

**Accessibility.** Error messages never rely on colour alone, the REPL works with screen readers, and indentation can be shown with visible guides (`nexo config indent-guides on`).

## 17. Numeric types and money

Nexo has three numeric types in the core and never mixes them silently. Money uses `decimal`, which is exact, and an amount with a currency is a `money`.

| Type | What it is | Use |
| --- | --- | --- |
| `int` | 64-bit integer; overflow is an error, it never wraps | counters, indices, quantities |
| `float` | 64-bit floating point (IEEE 754) | science, measurements, charts |
| `decimal` | exact 128-bit decimal, up to 28 significant digits | money, percentages, accounting |
| `money` | `decimal` + ISO 4217 currency | prices, invoices, payments |

**Rules**

1. A number with a decimal point is a `float` unless the variable, the parameter or the other operand is `decimal` or `money`.
2. Mixing `float` and `decimal` is a compile error; convert with `decimal(x)` or `float(x)`.
3. Adding `money` in different currencies is an error; convert with `convert(x, to: USD, rate: ...)`.
4. `/` always yields `float` between integers and `decimal` between decimals; `//` is integer division.
5. `round(x, 2)` rounds half to even (banker's rounding); `mode: half_up` changes it.
6. Splitting money never loses cents: `split` distributes the remainder across the first parts.

```nexo
price: decimal = 19.99
total = price * 3                      # 59.97, exact

invoice: money = 120.00 EUR
vat = invoice * 21%                    # 25.20 EUR
parts = split(100.00 EUR, 3)           # [33.34 EUR, 33.33 EUR, 33.33 EUR]

print(0.1 + 0.2)                       # 0.30000000000000004 (float)
x: decimal = 0.1
print(x + 0.2)                         # 0.3 (decimal)
```

Fixed-size types (`i8` to `u64`, `f32`) remain in `nexo.perf` for those who need them.

## 18. Interoperability

Nexo uses libraries from other languages from day one with a single form: `use <language> <name>`. Everything coming from another language is treated as external and is subject to the `policy`.

| Source | Form | How it works | Cost |
| --- | --- | --- | --- |
| C (and any language with a C ABI) | `use c "libssl"` | direct call with headers read automatically | none |
| Rust | `use rust serde_json` | crate compiled and linked into the same binary | none |
| Python | `use python numpy as np` | embedded Python interpreter; data is converted at the boundary | data conversion |
| WebAssembly | `use wasm "plugin.wasm"` | isolated module with its own permissions | small |

```nexo
use c "libz"
compressed = libz.compress(data)

use python pandas as pd
table = pd.read_excel("legacy.xlsx") |> to_table

use wasm "filter.wasm", allow: [read "images/"]
result = filter.apply(photo)
```

**The other way round.** `nexo build --lib` generates a library with a C ABI, a Python package (`--python`) or a WebAssembly module (`--target wasm`), so Nexo code can be used from other languages.

**Rules.** Calls to C count as implicit `unsafe` and `nexo audit` lists them; C functions with no declared errors return `T!` if the header indicates an error code. Unannotated Python types arrive as `any` and must be converted with `validate` before being used with Nexo types.

## 19. Real-time data (`nexo.stream`)

A data stream is just another source for `when` and `|>`; there is no new syntax. Each message is processed at least once: it is acknowledged to the broker only when the block finishes without error. For exactly-once effects, `when ... as msg, key: msg.id` discards messages already processed with that key.

| Source | Form |
| --- | --- |
| Kafka | `kafka("orders", servers: "...")` |
| RabbitMQ | `rabbit("queue")` |
| AWS SQS / Kinesis | `sqs("queue")`, `kinesis("stream")` |
| Azure Service Bus / Event Hubs | `servicebus("queue")`, `eventhub("stream")` |
| GCP Pub/Sub | `pubsub("topic")` |
| WebSocket | `websocket("wss://...")` |
| `queue` declared in `infra` | by its name: `orders` |

```nexo
use nexo.stream

# one message at a time
when kafka("orders") as msg:
    o: Order = validate(msg.json(), as: Order)?
    process(o)

# time windows with the same pipeline
kafka("clicks")
  |> window 1m
  |> group by page
  |> count
  |> each publish_metrics

# publish
kafka("notices").send({user: id, text: "Order shipped"})
```

**Rules.** If the block fails, the message is retried 3 times with exponential backoff and then goes to the dead-letter queue `<source>.dead`. `when` on a stream scales with implicit `parallel` per partition, preserving order within each one. `window` (tumbling window), `sliding` (sliding window) and `session` (inactivity window) are contextual pipeline steps.

## 20. GPU and model training (`nexo.ml`)

`ask` uses models; `nexo.ml` trains and fine-tunes them. Tensors are just another type and the code is the same on CPU and GPU: the target is chosen with a single option.

**Rules**

1. `tensor[float32, 3, 224, 224]` declares shape and type; operating on incompatible shapes is a compile error when the shape is known.
2. `device = auto` uses the available GPU (CUDA, ROCm or Metal), falling back to the CPU. It is set in the `ml` block or per call.
3. An `@gpu` function is compiled for the GPU; it may only use tensors, numbers and loops, with no I/O.
4. Gradients are automatic for any function on tensors (`grad(f)`).
5. Training and fine-tuning respect `budget` and `policy`: `@private` data never leaves the machine without permission.

```nexo
use nexo.ml

ml:
    device = auto
    budget = 20 EUR per run

# fine-tune a language model on your data
examples = read("support.jsonl")
my_model = finetune("ollama:llama", examples, epochs: 3)
r = ask("Classify", mail, model: my_model)

# train a classic model
data = read("customers.csv")
model = train(data, predict: "churn", kind: classifier)
print(model.report())                  # precision, recall, confusion matrix

# custom network
type Net is Model:
    layer1 = linear(784, 128)
    layer2 = linear(128, 10)

    fn forward(self, x: tensor):
        return x |> self.layer1 |> relu |> self.layer2

net = Net()
net.fit(images, labels, epochs: 10, loss: cross_entropy)
net.save("net.nexomodel")

@gpu
fn add(a: tensor, b: tensor) -> tensor:
    return a + b
```

`nexo ml track` records every training run with its data, parameters and metrics so it can be reproduced. A saved model is deployed like any `app` with `image = model("net.nexomodel")`.

## 21. Interactive development

`nexo dev` runs the program and updates it on save without restarting. Notebooks are ordinary `.nexo` files split into cells, so the same file runs as a program, is reviewed in Git and opens as a notebook.

**Hot reload**

1. On save, `nexo dev` recompiles only the changed functions and swaps them into the running program.
2. Global variables, open connections and `single` state are preserved.
3. If a `type` with live data changes, only that module is restarted and a warning says what state was lost.
4. If the new code does not compile, the previous version keeps running and the error is shown in the editor.
5. Hot reload only exists in development; `nexo build --release` does not include it.

**Notebooks**

```nexo
# %% Load data
sales = read("sales.parquet")
sales |> take 5 |> show

# %% Sales by region
summary = sales |> group by region |> sum amount
chart(summary, kind: bar)

# %% AI summary
ask("Explain the main trends", summary)
```

`# %% title` marks the start of a cell. `nexo notebook sales.nexo` opens it in the browser with results, tables and charts under each cell; `nexo sales.nexo` runs it end to end as a program. Results are not saved in the file (they are kept separately in `.nexo-cache/`), so version control only sees code. The format is Jupyter-compatible via `nexo notebook --export ipynb`.

## 22. Feature flags

A new feature is switched on or off in production without redeploying. Flags are declared in a `flags` block and read with the `flag()` function; there is no new keyword beyond the block.

```nexo
flags:
    new_checkout = 10%                         # stable percentage per user
    dark_mode = users where country == "ES"
    payments_v2 = off
    ai_recommendations = on, expires: 2027-03-01

if flag("new_checkout", user):
    checkout_v2(cart)
else:
    checkout(cart)
```

**Rules**

1. The same user always gets the same result for a given percentage, even across servers.
2. Using an undeclared flag is a compile error.
3. A flag whose `expires` date has passed triggers a `nexo lint` warning to remove it from the code.
4. `nexo flags set new_checkout 50%` changes the value live across every environment of the chosen stage, and the change is recorded with `audit`.
5. Flags can watch metrics like a canary: `new_checkout = 10%, watch: [errors < 1%], rollback: auto`.

Flag state is stored in the service set in `infra` (`flags_store = "redis"` or each cloud's native manager) and, with no infrastructure, in a local file.

## 23. Privacy and GDPR

Nexo knows which data is personal because it is marked in the types, so it can list where it lives, how long it is kept and delete it per person. These tools help with GDPR compliance but do not replace legal judgement.

**Markers in types**

```nexo
@table
type Customer:
    id: id
    name: str @personal
    email: str @personal
    national_id: str @private                  # personal and sensitive: encrypted at rest
    orders: many Order

    keep = 5 years after last_activity          # retention period
    purpose = "billing and shipping"            # declared purpose
```

`@personal` identifies a person; `@private` is personal and sensitive, is encrypted at rest and never appears in logs or AI calls.

**Data subject rights**

| Right | Code |
| --- | --- |
| Access and portability | `privacy.export(customer)` produces a JSON file with all their data, including relations |
| Erasure | `privacy.erase(customer)` deletes or anonymises across all tables, caches and marked backups |
| Rectification | `db.Customer.update(...)`, with automatic `audit` |
| Restriction | `privacy.restrict(customer)` blocks use except for legal obligations |

**Rules**

1. A `type` with `@personal` fields but no `keep` or `purpose` is a `nexo lint` warning; in `release` it is an error.
2. Data past its `keep` period is deleted or anonymised by an automatically generated daily task.
3. Sending `@personal` data outside the region declared in `infra` (`data_region = "eu"`) is a compile error unless the `policy` explicitly allows it.
4. `nexo privacy report` lists every personal field, where it is stored, who can read it, its retention period and its purpose: it is the basis of the record of processing activities.

## 24. Formal grammar of the core

This grammar defines the core of edition 0.1; module blocks (`ai`, `infra`, `pipeline`...) all follow the generic `config_block` rule. Any difference between this text and the compiler is considered a compiler bug.

**Lexical rules**

1. UTF-8 encoding. Identifiers accept Unicode letters (accents and ñ included), digits and `_`, and do not start with a digit.
2. Indentation is exactly 4 spaces; a tab is an error. The lexer emits `INDENT` and `DEDENT` when the level changes.
3. A newline ends a statement, except inside `( )`, `[ ]`, `{ }` or when the next line starts with `|>`.
4. `#` starts a comment until the end of the line; `##` is a documentation comment.
5. Numbers: `1_000`, `3.14`, `1e-3`. A number followed by a unit or currency (`5 km`, `30s`, `19.99 EUR`, `10%`) forms a unit literal.
6. Text: `"..."` with `{expr}` interpolation, `"""..."""` for multi-line text and `{{` for a literal brace.

**Grammar (EBNF)**

```ebnf
program       = { statement } ;
block         = ":" NEWLINE INDENT statement { statement } DEDENT ;

statement     = simple_stmt NEWLINE | compound_stmt ;
simple_stmt   = assignment | var_decl | return_stmt | expr | "pass"
              | use_stmt | emit_stmt | expect_stmt | ensure_stmt ;
compound_stmt = fn_def | type_def | trait_def | if_stmt | for_stmt
              | while_stmt | repeat_stmt | match_stmt | try_stmt
              | parallel_stmt | test_def | event_stmt | config_block ;

assignment    = target [ ":" type ] ( "=" | "+=" | "-=" | "*=" | "/=" ) expr ;
var_decl      = "var" IDENT [ ":" type ] "=" expr ;
target        = IDENT { "." IDENT | "[" expr "]" } ;
return_stmt   = "return" [ expr ] ;
use_stmt      = "use" [ IDENT ] dotted_name [ "as" IDENT ] ;
emit_stmt     = "emit" call ;
expect_stmt   = "expect" expr ;
ensure_stmt   = "ensure" expr ;

fn_def        = { annotation } [ "pub" ] "fn" IDENT "(" [ params ] ")" [ "->" type ] block ;
params        = param { "," param } ;
param         = IDENT [ ":" type ] [ "=" expr ] ;
annotation    = "@" dotted_name [ "(" [ args ] ")" ] NEWLINE ;

type_def      = { annotation } [ "pub" ] "type" IDENT [ type_params ]
                ( "=" type NEWLINE | [ "is" dotted_name ] type_body ) ;
type_body     = ":" NEWLINE INDENT { field | variant | fn_def | config_pair } DEDENT ;
field         = IDENT ":" type [ "=" expr ] { annotation_inline } NEWLINE ;
variant       = IDENT [ "(" params ")" ] NEWLINE ;
trait_def     = "trait" IDENT ":" NEWLINE INDENT { fn_sig NEWLINE } DEDENT ;
fn_sig        = "fn" IDENT "(" [ params ] ")" [ "->" type ] ;

type          = base_type [ "?" | "!" ] | literal { "|" literal } ;
base_type     = dotted_name [ type_params ] | "weak" base_type ;
type_params   = "[" type { "," type } "]" ;

if_stmt       = "if" expr block { "elif" expr block } [ "else" block ] ;
for_stmt      = "for" IDENT "in" expr block ;
while_stmt    = "while" expr block ;
repeat_stmt   = "repeat" expr block ;
match_stmt    = "match" expr ":" NEWLINE INDENT match_arm { match_arm } DEDENT ;
match_arm     = pattern { "or" pattern } [ "if" expr ] ( ":" simple_stmt NEWLINE | block ) ;
pattern       = "_" | literal | IDENT [ "(" pattern { "," pattern } ")" ] ;
try_stmt      = "try" [ "within" expr ] block else_clause { else_clause } ;
else_clause   = "else" IDENT [ "is" dotted_name ] block ;
parallel_stmt = "parallel" block ;
test_def      = "test" STRING block ;
event_stmt    = ( "on" | "when" ) call [ "as" IDENT ] block
              | "every" expr [ "at" expr ] block
              | "lifecycle" IDENT [ "," args ] block ;

config_block  = IDENT [ IDENT | STRING ] ":" NEWLINE INDENT
                { config_pair | config_line | config_block } DEDENT ;
config_pair   = IDENT "=" expr { "," arg } NEWLINE ;
config_line   = IDENT { expr | IDENT } NEWLINE ;

expr          = lambda | pipeline ;
lambda        = IDENT "->" expr ;
pipeline      = or_expr { "|>" pipe_step } ;
pipe_step     = IDENT { expr | IDENT } | call ;
or_expr       = and_expr { "or" and_expr } ;
and_expr      = not_expr { "and" not_expr } ;
not_expr      = "not" not_expr | comparison ;
comparison    = coalesce { ( "==" | "!=" | "<" | ">" | "<=" | ">=" | "in" | "is" ) coalesce } ;
coalesce      = range_expr { "??" range_expr } ;
range_expr    = sum [ ".." sum ] ;
sum           = term { ( "+" | "-" ) term } ;
term          = power { ( "*" | "/" | "//" | "%" ) power } ;
power         = unary [ "**" power ] ;
unary         = "-" unary | postfix ;
postfix       = primary { "." IDENT | "(" [ args ] ")" | "[" expr "]" | "?" } ;
primary       = literal | IDENT | "(" expr ")" | list_lit | map_lit | comprehension
              | "parallel" "for" IDENT "in" expr [ "," args ] block ;
args          = arg { "," arg } ;
arg           = [ IDENT ":" ] expr ;
literal       = NUMBER [ UNIT ] | STRING | "true" | "false" | "none" ;
list_lit      = "[" [ expr { "," expr } ] "]" ;
map_lit       = "{" [ expr ":" expr { "," expr ":" expr } ] "}" ;
comprehension = "[" expr "for" IDENT "in" expr [ "if" expr ] "]" ;
dotted_name   = IDENT { "." IDENT } ;
```

**Precedence** (lowest to highest): `->`, `|>`, `or`, `and`, `not`, comparisons, `??`, `..`, `+ -`, `* / // %`, `**`, unary `-`, member access and call. `%` right after a number is a unit (percentage); between two expressions it is the remainder operator.

## 25. Implementation plan

Nexo is built in phases and each one ends with something usable. The first compiler is written in Rust and generates code with LLVM; once the language is stable, the compiler is rewritten in Nexo itself.

| Phase | Deliverable | Exit criterion |
| --- | --- | --- |
| 0. Prototype | Python interpreter for the core: variables, control flow, functions, `type`, errors with `?` and `try`, pipelines | the examples in sections 2, 3 and 6 run; 20 people try it and the syntax is adjusted |
| 1. Base compiler | Rust compiler: lexer, parser (section 24), type checking, LLVM | compiles the core to native code; conformance tests pass at 100% |
| 2. Memory and concurrency | memory model (section 7), `parallel`, `nexo.sync`, contracts | no leaks or races in the test suite; benchmarks under 2× Rust |
| 3. Tooling | `nexo fmt`, `lint`, `test`, `lsp`, error messages, REPL | VS Code extension published; every error has a proposed fix |
| 4. Library and modules | standard library, `nexo.ai`, `nexo.web`, `nexo.db`, C and Python interoperability | a web API with a database and `ask` is built with Nexo alone |
| 5. Cloud and operations | `nexo.infra`, `nexo.ci`, `nexo.stream`, flags, privacy | an app deploys to AWS, Azure, GCP and k8s from the same file |
| 6. Optimisation and 1.0 | performance goals, external audit, user studies | the guarantees in "Goals and guarantees" are met or removed |
| 7. Self-hosting | compiler rewritten in Nexo | the Nexo compiler compiles itself and passes every test |

**Conformance tests.** From phase 1 there is a public repository of test programs with their expected output. Any Nexo implementation (including the official one) is correct only if it passes all of them.

**Reuse.** To move fast, proven components are used: LLVM for code generation, Tokio for non-blocking I/O, Apache Arrow for columnar tables and each cloud's official SDKs.

## 26. Ecosystem

A language takes off because of its ecosystem as much as its design. These pieces exist from the first public release.

| Piece | What it is | Decision |
| --- | --- | --- |
| MCP server | `nexo mcp` exposes compiling, testing, documenting and explaining code to any AI assistant | included in the `nexo` binary |
| Web playground | write and run Nexo in the browser, compiled to WebAssembly, with a shareable link | `play.nexo-lang.org` (provisional name) |
| Package registry | signed packages, with auditing and visible permissions | a single official registry |
| Licence | compiler, library and official modules | Apache 2.0 with LLVM exception |
| Governance | language changes through public proposals (NEP) with a comment period | non-profit foundation once there are several sponsors |
| Documentation | guide, reference, examples and `nexo learn`, in Spanish and English from the start | part of the main repository |
| Community | forum, chat and open monthly meetings | published code of conduct |

**MCP server**

```bash
nexo mcp            # starts the server for the current project
```

Tools it offers an assistant: `check` (compile and return errors with their fix), `run_tests`, `explain`, `docs_for_ai`, `plan` (infrastructure plan, without applying it) and `format`. It never exposes `apply`, `deploy` or secrets; those actions still require a person.

**Change proposals (NEP).** Anyone can propose a change with a document covering motivation, syntax, alternatives and migration. After 30 days of comments, the core team accepts or rejects it with a public explanation. An accepted change that breaks code only lands in the next edition (section 13).

## 27. Reserved words

The core has 39 words reserved everywhere in the code. Module words are only reserved at the start of a line or inside their module's block, so they can still be used as variable names elsewhere.

| Group | Words | Scope |
| --- | --- | --- |
| Control | `if`, `elif`, `else`, `for`, `in`, `while`, `repeat`, `match`, `pass` | core, always reserved |
| Definition | `fn`, `return`, `type`, `trait`, `is`, `pub`, `use`, `var`, `self`, `weak` | core, always reserved |
| Values and logic | `true`, `false`, `none`, `and`, `or`, `not`, `as` | core, always reserved |
| Errors and contracts | `try`, `unsafe`, `expect`, `ensure` | core, always reserved |
| Concurrency and events | `parallel`, `on`, `emit`, `when`, `every`, `at`, `lifecycle` | core, always reserved |
| Testing | `test`, `assert` | core, always reserved |
| Patterns | `single`, `states`, `action`, `undo`, `protect`, `workflow`, `step` | contextual |
| Pipelines | `where`, `map`, `pick`, `group`, `by`, `sum`, `count`, `avg`, `sort`, `desc`, `take`, `each`, `it` | contextual, only inside a pipeline |
| AI | `ai`, `agent`, `models`, `profile` | contextual (`nexo.ai`) |
| Security | `policy`, `allow`, `deny`, `require`, `rotate` | contextual |
| Infrastructure | `infra`, `app`, `server`, `database`, `storage`, `cache`, `queue`, `cluster`, `network`, `dns`, `identity`, `native`, `output`, `stage`, `env` | contextual (`nexo.infra`, `nexo.web`) |
| CI and deployment | `pipeline`, `trigger`, `after`, `release`, `scale`, `calls` | contextual (`nexo.ci`) |
| Advanced | `spawn`, `channel`, `select`, `shared`, `arena` | contextual (`nexo.sync`, `nexo.perf`) |
| Streaming, ML and flags | `window`, `sliding`, `session`, `ml`, `flags` | contextual (`nexo.stream`, `nexo.ml`, flags) |

**Removed compared with earlier drafts:** `let`, `struct`, `enum`, `impl`, `await`, `share`, `store`, `choose`, `make`, `with`, `view`, `move`, `copy`, `filter`, and the uses of `on` for clouds, CI and shutdown.

**Open for edition 1.0:** the graphical syntax of `nexo.ui` and the exact format of the `nexo.db` migration file.
