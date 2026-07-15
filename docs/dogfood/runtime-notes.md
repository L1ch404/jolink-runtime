# Runtime Dogfood Notes: Scenario Trigger and Request Parameter Sources

> Status: working notes
> Scope: Java Runtime / Debug Session dogfood
> Purpose: record practical problems found during dogfood, especially how to trigger real backend code paths and where HTTP request parameters should come from.

---

## 1. Background

The current Runtime was originally created to give an LLM the ability to debug a real Java program.

The basic idea is:

```text
LLM / Agent
    ↓
Runtime Action
    ↓
Java Runtime
    ↓
Running Java Application
```

The Runtime can manage and observe a running Java process:

```text
run
stop
restart
status
set_breakpoint
wait_breakpoint
threads
stack
variables
resume
```

However, during dogfood, one important limitation became obvious:

> Runtime can observe a program after it reaches a certain state, but it does not automatically create the business scenario that makes the program reach that state.

For a real Java backend service, the target code path is usually not reached by simply running `main()`.

It is usually reached through:

```text
HTTP request
    ↓
authentication / permission check
    ↓
parameter validation
    ↓
Controller
    ↓
Service
    ↓
Database / Redis / remote services
    ↓
target breakpoint
```

Therefore, Runtime alone is not enough. We also need a way to trigger the right business path.

---

## 2. Core Distinction

Runtime answers:

> What is the real state of the program when it is running?

Scenario triggering answers:

> How do we make the program run to the target position?

These are different responsibilities.

```text
Scenario Builder / Trigger
    ↓
HTTP request / test data / DB data
    ↓
Running Java Application
    ↓
Runtime
    ↓
breakpoint / stack / variables / logs
```

So the current design should avoid mixing the two concepts too early.

Runtime should remain focused on:

```text
process lifecycle
debug session lifecycle
breakpoint events
stack frames
variables
logs
observation validity
```

Scenario triggering may later become a separate layer or helper, but it should not be forced into Runtime itself.

---

## 3. HTTP Request Parameter Sources

When debugging a real Java backend, the Agent often needs to call an HTTP API to hit the breakpoint.

The hard part is not only knowing the URL. It also needs valid request parameters.

Possible sources are listed below.

---

### 3.1 User-provided request

This is the most stable source in early dogfood.

The user provides:

```text
curl command
Postman request
request body JSON
query parameters
headers
token
userId / orderId / business identifiers
```

Example:

```bash
curl -X POST "http://localhost:8080/order/submit" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer xxx" \
  -d '{
    "orderId": 1001,
    "userId": 88
  }'
```

Advantages:

- Highest success rate.
- User already knows the business context.
- Avoids the Agent inventing invalid business data.
- Best fit for the first dogfood stage.

Disadvantages:

- Requires user participation.
- Agent is not yet fully autonomous.

Dogfood priority:

```text
P0
```

Current recommendation:

> In the first dogfood stage, let the user provide the request or curl. Runtime should focus on observing the program after the request reaches the breakpoint.

---

### 3.2 Existing logs and historical requests

The Agent may find valid request examples from existing logs.

Sources may include:

```text
application logs
access logs
gateway logs
Nginx logs
debug logs
previous curl commands
recorded request bodies
```

Example log:

```text
POST /order/submit {"orderId":1001,"userId":88}
```

Advantages:

- Often contains real business data.
- More reliable than generating parameters from scratch.
- Can help reproduce real bugs.

Disadvantages:

- Logs may be incomplete.
- Sensitive data must be handled carefully.
- The request may depend on expired tokens or outdated state.

Dogfood priority:

```text
P1
```

Notes:

- Prefer read-only log parsing.
- Avoid leaking or permanently storing sensitive data.
- If logs contain tokens or credentials, mask them before returning to the LLM when possible.

---

### 3.3 API documents, Swagger / OpenAPI, Postman, frontend code

The Agent may infer request structure from API descriptions or caller code.

Possible sources:

```text
Swagger / OpenAPI
Postman Collection
Apifox documents
frontend request code
TypeScript API client
README
integration test
controller annotations
```

Example Java Controller:

```java
@PostMapping("/order/submit")
public Result submit(@RequestBody OrderSubmitReq req) {
    ...
}
```

The Agent may infer the request body from:

```text
DTO fields
validation annotations
enum values
comments
Swagger annotations
frontend caller code
```

Advantages:

- Good at discovering URL, method, headers, DTO shape.
- Helps build a syntactically valid request.
- Useful when the user does not provide curl.

Disadvantages:

- It usually cannot guarantee the business data is valid.
- It may know the field names, but not know which `orderId` actually exists.
- It may miss authentication, tenant, environment, or permission requirements.

Dogfood priority:

```text
P1
```

Recommended use:

> Use this source to infer request shape, but do not assume the generated values are valid.

---

### 3.4 Infer from source code

The Agent can inspect backend code directly.

Useful code locations:

```text
Controller method
Request DTO
validation annotations
Service branch condition
Enum definitions
Mapper / Repository calls
permission checks
constant definitions
unit tests
integration tests
```

Example:

```java
if (order.getStatus() != OrderStatus.PAID) {
    throw new BusinessException("Only paid orders can be submitted");
}
```

From this, the Agent learns that it needs an order whose status is `PAID`.

Advantages:

- Helps understand business constraints.
- Helps decide what kind of data is needed.
- Useful for choosing breakpoint location and request parameters.

Disadvantages:

- Still may not know where to get real matching data.
- Static reasoning may be wrong when runtime configuration or database state differs.
- Can lead to over-guessing.

Dogfood priority:

```text
P1
```

Recommended use:

> Use code inference to understand constraints, then combine it with user-provided data, logs, tests, or database queries.

---

### 3.5 Read-only database query

The Agent may query the database to find valid existing business data.

Example:

```sql
SELECT id, status, user_id
FROM orders
WHERE status = 'PAID'
LIMIT 5;
```

Advantages:

- Can find real valid IDs and states.
- Very useful when request parameters must reference existing records.
- Helps avoid invalid synthetic data.

Disadvantages:

- Requires DB connection configuration.
- Schema may be complex.
- The Agent may query too broadly.
- There are security and data sensitivity risks.
- Write operations can damage the environment.

Dogfood priority:

```text
P2
```

Strict rule for early stages:

```text
SELECT only
LIMIT required
No INSERT
No UPDATE
No DELETE
No DDL
No stored procedure execution
No destructive operation
```

Recommended constraints:

```text
read-only database user
query timeout
row limit
table allowlist
sensitive field masking
explicit user confirmation for risky queries
```

Important distinction:

> Database query is not Runtime itself. It is a scenario/data helper that helps construct a valid request.

---

### 3.6 Generate parameters from scratch

The LLM may generate request parameters without external evidence.

Example:

```json
{
  "name": "test",
  "age": 18
}
```

Advantages:

- Fast.
- Useful for very simple APIs.
- Good for toy examples or pure validation endpoints.

Disadvantages:

- Usually unreliable for real business systems.
- Generated IDs may not exist.
- Generated status may violate business rules.
- Missing authentication, tenant, permission, or environment constraints.
- May waste time with repeated invalid requests.

Dogfood priority:

```text
P3 / fallback only
```

Recommended rule:

> The Agent may generate parameters from scratch only when the endpoint is simple or when no better source exists. It should clearly mark the request as a guess.

---

## 4. Suggested Priority Order

For dogfood, the request parameter source priority should be:

```text
1. User-provided curl or request body
2. Existing logs / historical requests
3. API docs / Swagger / Postman / frontend code
4. Source code inference
5. Read-only database query
6. LLM-generated synthetic request
```

The first stage should prefer:

```text
user-provided request
+
Runtime observation
```

This keeps the first dogfood loop simple.

---

## 5. Dogfood Workflow

A practical first-stage workflow:

```text
1. User provides:
   - project path
   - launch command or classpath/main class
   - breakpoint class and line
   - curl or request body

2. Runtime starts Java application.

3. Runtime sets breakpoint.

4. Trigger sends HTTP request.

5. Runtime waits for breakpoint hit.

6. Runtime returns:
   - suspension_id
   - source location
   - thread
   - stack
   - variables
   - nearby logs

7. Agent analyzes the state.

8. Runtime resumes program.

9. Agent modifies code or proposes fix.

10. User or Agent reruns the request to verify.
```

The initial goal is not full autonomy.

The initial goal is:

> The user provides the business trigger, and the Runtime provides reliable observation.

---

## 6. Important Problem: HTTP Trigger May Block the Agent

A real backend breakpoint can block the HTTP request.

If the Agent calls HTTP synchronously:

```text
Agent calls HTTP API
    ↓
request enters Java program
    ↓
thread hits breakpoint and pauses
    ↓
HTTP call waits for response
    ↓
Agent cannot call wait_breakpoint
```

This can deadlock the workflow at the tool level.

Therefore, the trigger mechanism should not always be a blocking HTTP call.

Possible solutions:

### 6.1 User triggers manually

The user runs curl/Postman manually after the breakpoint is set.

```text
Runtime set breakpoint
    ↓
User sends request
    ↓
Agent calls wait_breakpoint
```

This is simple and suitable for early dogfood.

### 6.2 Async HTTP trigger

Provide a trigger tool that starts the request in the background and immediately returns a task ID.

```text
trigger_http_async
    ↓
returns trigger_task_id
    ↓
wait_breakpoint
    ↓
resume
    ↓
read trigger result
```

### 6.3 Shell background command

Use a background `curl` or script.

Example:

```bash
curl ... &
```

This is platform-dependent and should be handled carefully on Windows.

### 6.4 Combined trigger-and-wait

A higher-level tool could do:

```text
send request in background
+
wait for breakpoint
```

But this should be built after the basic Runtime behavior is stable.

---

## 7. Relationship Between Runtime and Scenario Builder

Runtime should not become a giant tool that does everything.

A cleaner mental model:

```text
Agent
├── Scenario Builder
│   ├── read API docs
│   ├── read logs
│   ├── inspect controller
│   ├── query database read-only
│   └── build candidate request
│
└── Runtime
    ├── run process
    ├── set breakpoint
    ├── wait breakpoint
    ├── inspect stack
    ├── inspect variables
    └── resume
```

Runtime observes execution.

Scenario Builder creates execution.

They can cooperate, but their responsibilities should remain separate.

---

## 8. Dogfood Finding: Windows Compatibility

During company dogfood, the Runtime did not run smoothly on Windows.

This is not a failure of the Runtime idea. It is an early platform compatibility finding.

Common Windows issues include:

```text
path separator
classpath separator
shell command differences
process termination differences
encoding issues
java / javac / jps / jcmd path discovery
firewall and port behavior
```

Examples:

### Classpath separator

Do not hardcode:

```text
:
```

Use:

```python
os.pathsep
```

Windows:

```text
;
```

macOS / Linux:

```text
:
```

### Path handling

Use:

```python
Path(...)
```

Avoid manual string concatenation.

### Process control

Unix may use:

```text
kill
process group
SIGTERM
```

Windows may need:

```text
taskkill /PID <pid> /T /F
```

### Encoding

Windows console output may use:

```text
cp936
GBK
UTF-8
```

Subprocess decoding should avoid crashing on invalid bytes.

Possible strategy:

```text
try UTF-8
fallback to locale encoding
replace invalid characters
```

### Java command discovery

Do not assume `java`, `javac`, `jps`, `jcmd` are always in PATH.

Possible sources:

```text
JAVA_HOME
PATH
IDEA configured JDK
explicit user-provided JDK path
```

---

## 9. Suggested Platform Adapter

To avoid scattering `if windows` everywhere, introduce a small platform abstraction.

```text
PlatformAdapter
├── find_java()
├── find_javac()
├── find_jps()
├── find_jcmd()
├── build_classpath()
├── normalize_path()
├── start_process()
├── stop_process()
├── list_java_processes()
└── decode_output()
```

Runtime should call PlatformAdapter instead of directly depending on platform-specific commands.

The first Windows dogfood target should be very small:

```text
1. run simple Java main
2. logs are readable
3. breakpoint can be hit
4. variables can be read
5. stop does not leave orphan processes
```

Do not start with complex Spring attach or full project integration.

---

## 10. Runtime Observation Trust Rules

During dogfood, one important issue was identified:

> A missing variable value must not be represented as a real `null`.

For example:

```json
{
  "name": "user",
  "value": null
}
```

This should mean:

> Runtime successfully observed that the Java value is actually null.

If Runtime failed to read the value, it should return an error instead:

```json
{
  "name": "user",
  "error": "Failed to read variable value: JDWP error 35"
}
```

Simple dogfood rule:

```text
value means successfully observed
error means not observed
value and error should not appear at the same time
```

This keeps the early implementation simple while avoiding a dangerous semantic bug.

---

## 11. Current Dogfood Strategy

Current stage:

```text
internal dogfood
human watching
failure allowed
fix while using
```

Not current stage:

```text
stable external user release
general Agent Runtime
multi-language runtime
autonomous debugging product
```

Dogfood success criteria:

```text
The user can use Hermes + Runtime to complete one real Java debugging flow
with limited manual rescue.
```

Recommended first dogfood scope:

```text
single Java target
Runtime launches the process
user provides curl/request
one breakpoint
wait immediately after trigger
read stack and variables
resume explicitly
```

Avoid in the first stage:

```text
external attach
pending breakpoint
multi-target
deep object graph
same-name threads
arbitrary expression evaluation
database write
complex async workflows
```

---

## 12. Key Takeaways

1. Runtime solves observation, not scenario creation.

2. Real Java backend debugging requires a trigger path, usually an HTTP request.

3. HTTP request parameters can come from user input, logs, API docs, source code, read-only DB queries, or LLM generation.

4. The first dogfood stage should rely on user-provided curl/request parameters.

5. Database access may become useful, but it should be read-only and separate from Runtime.

6. Synchronous HTTP calls can block when a breakpoint is hit, so async triggering or manual triggering may be needed.

7. Windows compatibility should be treated as a first-class dogfood issue.

8. Runtime should not return fake facts. If a value cannot be read, return an error instead of `null`.

9. The current goal is not to prove Runtime is a universal infrastructure layer. The current goal is to make it useful enough for real self-use.

10. Dogfood is the mechanism for discovering which parts of the Runtime are actually needed.

---

## 13. Open Questions

- Should Scenario Builder become a separate module later?
- Should HTTP triggering be part of Runtime, or a separate tool?
- What is the minimal async trigger design?
- Which parameter source should the Agent try first when the user does not provide curl?
- How should sensitive request data and database rows be masked?
- Can logs provide enough request samples for most internal debugging?
- When should the Agent ask the user for parameters instead of guessing?
- How should Runtime associate a request, breakpoint hit, logs, and variables into one debug episode?
- Should Runtime expose nearby logs around the breakpoint time?
- What is the smallest benchmark that can test this full flow?


# Case-002 Runtime-assisted Code Reading

## 目标
验证 Runtime 是否能辅助 LLM 在不大量静态读源码的情况下理解一次 HTTP 请求链路。

## 方法
触发公司项目随机 API，通过断点暂停请求线程，读取 30 层 stack，并在多个 frame_index 上读取 variables。

## 结果
- stack 展示 NIO socket → CoyoteAdapter → Tomcat Valve → Filter chain → Controller 的完整路径。
- SpringBootFilter frame 中观察到 requestId、sessionid、isDebug、requestWrapper body。
- CORSFilter frame 中通过变量确认其标准 CORS 过滤职责。
- stack + frame variables 能帮助 LLM 快速理解请求链路和中间层行为。

## 发现的问题
- SUSPEND_ALL 多轮后会残留，导致新请求断点不命中。
- 多断点 + 多请求并发需要精确 request_id 管理。
- exception event 对 NPE 定位效率远高于逐行断点。

## 结论
Runtime 不只适合 Debug，也适合运行时辅助读代码。
