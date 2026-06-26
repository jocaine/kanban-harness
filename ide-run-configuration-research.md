# IDE Run Configuration Design Patterns Research

## 1. VS Code: launch.json + tasks.json

### Sources
- https://code.visualstudio.com/docs/debugtest/debugging-configuration
- https://code.visualstudio.com/docs/editor/debugging
- https://code.visualstudio.com/docs/editor/tasks
- https://code.visualstudio.com/docs/editor/variables-reference
- https://code.visualstudio.com/docs/cpp/launch-json-reference

### Architecture: Two-File Separation

**launch.json** — Debug/Run configurations (stored in `.vscode/`)
**tasks.json** — Build/automation tasks (stored in `.vscode/`)

This is the key decoupling: *build* is separate from *run/debug*.

### launch.json Schema Design

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "type": "node",           // Debug adapter type (extension-contributed)
      "request": "launch",      // "launch" or "attach"
      "name": "Launch Program", // User-facing label
      "program": "${workspaceFolder}/app.js",
      "args": ["arg1", "arg2"],
      "env": { "NODE_ENV": "development" },
      "cwd": "${workspaceFolder}",
      "preLaunchTask": "npm: build",  // Links to tasks.json
      "postDebugTask": "cleanup",
      "console": "integratedTerminal", // or "externalTerminal", "internalConsole"
      "skipFiles": ["<node_internals>/**"]
    }
  ],
  "compounds": [
    {
      "name": "Server/Client",
      "configurations": ["Launch Server", "Launch Client"],
      "preLaunchTask": "build-all",
      "stopAll": true
    }
  ]
}
```

**Core schema fields (all types):**
- `type` — adapter identifier (node, cppdbg, cppvsdbg, python, go,e` — display name
- `preLaunchTask` / `postDebugTask` — link to tasks.json entries
- `presentation` — UI grouping/ordering

**Type-specific fields (examples):**
- C/C++: `program`, `MIMode` (gdb/lldb), `miDebuggerPath`, `externalConsole`, `symbolSearchPath`
- Node.js: `program`, `runtimeExecutable`, `runtimeArgs`, `console`, `outputCapture`
- Python: `module`, `justMyCode`, `django`, `flask`
- Remote: `port`, `address`, `localRoot`, `remoteRoot`

### tasks.json Schema Design

```json
{
  "version": "2.0.0",
  "tasks": [
    {
      "label": "build",
      "type": "shell",          // "shell", "process", or contributed (npm, typescript, etc.)
      "command": "gcc",
      "args": ["-g", "${file}", "-o", "${fileDirname}/${fileBasenameNoExtension}"],
      "group": { "kind": "build", "isDefault": true },
      "problemMatcher": ["$gcc"],
      "presentation": { "reveal": "always", "panel": "shared" },
      "dependsOn": ["pre-build"],
      "dependsOrder": "sequence"  // or "parallel"
    }
  ]
}
```

**Task groups:** `build`, `test` — allows default build/t Parse output into diagnostics (errors/warnings)

### Variable Substitution System

| Variable | Description |
|----------|-------------|
| `${workspaceFolder}` | Root folder path |
| `${file}` | Current file path |
| `${fileBasenameNoExtension}` | Filename without ext |
| `${selectedText}` | Editor selection |
| `${env:VAR}` | Environment variable |
| `${config:setting}` | VS Code setting |
| `${command:commandId}` | Dynamic from extension |
| `${input:inputId}` | User prompt at runtime |

### Program Types Supported
- **Web**: Node.js, Chrome/Edge debugging, Flask/Django
- **CLI**: Any language via appropriate debug adapter
- **Native GUI**: C/C++ (cppdbg/cppvsdbg), .NET
- **Compile-only**: Via tasks.json `build` group (no launch needed)
- **Embedded**: Cortex-Debug extension, OpenOCD, J-Link adapters
- **Remote**: Remote attach, SSH tunneling, Docker containers

### Build System Deco`preLaunchTask` links launch configs to build tasks by label
- Tasks can be `shell` (arbitrary command) or typed (npm, tsc, etc.)
- Extensions contribute task types and auto-detectirrides: `"linux"`, `"osx"`, `"windows"` keys
- Input variables for runtime prompts (pickString, promptString)
- Compound configurations for multi-target launches
- `presentation` controls terminal behavior

---

## 2. JetBrains IntelliJ: Run/Debug Configurations

### Sources
- https://www.jetbrains.com/help/idea/run-debug-configuration.html
- https://www.jetbrains.com/help/idea/creating-and-editing-run-debug-configurations.html
- https://www.jetbrains.com/help/idea/list-of-run-debug-configurations.html

### Architecture: Template-Based Model

JetBrains uses a **typed template system** where each run configuration is an instance of a configuration type (template). Plugins contribute configuration types.

### Two Configuration Lifecycles
1. **Temporary** — auto-created when running a class/method without existing config (max 5, auto-pruned)
2. **Permanent** — explicitly created from template or saved from temporary

### Configuration Type Categories (from official docs)

| Category | Types |
|----------|-------|
| JVM Application | Application, JAR Application, Kotlin, Scala |
| Web/Server | Tomcat (local/remote), Spring Boot, Node.js |
| Build Tools | Gradle, Maven |
| Testing | JUnit, TestNG, Karma, Mocha, Protractor |
| Scripting | Groovy, Kotlin Script, Scala REPL |
| JavaScript | Node.js, npm, Gulp.js, Grunt.js, JavaScript Debug |
| Docker | Dockerfile, Docker Image,Main class / entry point** (or script/command)
- **Module / classpath** context
- **VM options** / interpreter arguments
- **Program arguments**
- **Working directory**
- **Environment variables**
- **Before launch** tasks (ordered list): Build, Run Maven Goal, Run Gradle task, Run another config, etc.
- **Logs** tab: log files to show in console
- **Coverage** settings
- **Startup/Connection** settings (for remote)

### Storage & Sharing
- Stored as XML in `.idea/runConfigurations/` (when "Store as project file" is checked)
- Or in `.idea/workspace.xml` (user-local, Build System Decoupling: "Before Launch" Tasks

The "Before Launch" section is an ordered list of actions:
1. Build (IntelliJ's internal build)
2. Build, no error contributed)

This is analogous to VS Code's `preLaunchTask` but more structured — it's a pipeline, not a single task reference.

### Customization
- Templates act as defaults for all new configs of that type
- "Defaults" node in Run/Debug Configurations dialog
- Environment variables with per-config or shared profiles
- Macros/path variables in fields
- CompoConfiguration Schema

```js tests",
    "command": "cargo test",
    "args": [],
    "env": { "RUST_LOG": "debug" },
    "cwd": "/path/to/dir",
    "use_new_terminal": false,
    "allow_concurrent_runs": false,
    "reveal": "always",       // "always" | "no_focus" | "never"
    "hide": "never",          // "never" | "always" | "on_success"
    "shell": "system",        // or { "program": "bash" } or { "with_arguments": {...} }
    "show_summary": true,
    "show_command": true,
    "save": "none",           // "all" | "current" | "none"
    "tags": ["rust-test"]
  }
]
```

### Task Sources (priority order)
1. **Language extensions** — contribute task templates (e.g., cargo for Rust)
2. **Worktree-local** — `.zed/tasks.json` (project-specific)
3. **Global** —d-hoc tasks created via modal (don't persist across sessions)

### Variable System

| Variable | Description |
|----------|-------------|
| `ZED_FILE` | Absolute path of current file |
| `ZED_FILENAME` | Filename only |
| `ZED_DIRNAME` | Directory of current file |
| `ZED_RELATIVE_FILE` | Relative to worktree root |
| `ZED_STEM` | Filename without extension |
| `ZED_SYMBOL` | Currently selected symbol |
| `ZED_SELECTED_TEXT` | Selected text |
| `ZED_ROW` / `ZED_COLUMN` | Cursor position |
| `ZED_WORKTREE_ROOT` | Project root |
| `ZED_CUSTOM_<name>` | User-defined via modal prompts |

### Program Types Supported
- **All types via shell commands** — no type discrimination
- No built-in debug protocol integration (as of docs reviewed)
- Language extensions provide contextual task templates (e.g., "cargo run", "cargo test")
- **Inline runnables** — detected via `tags` in source (e.g., test annotations)

### Build System Decoupling
- Tasks ARE shell commands — no separate build vs. run concept
- No `preLaunchTask` equivalent; users compose via shell (`&&` chaining)
- `dependsOn` not supported — simpler model

### Customization
- `reveal` / `hide` control terminal UX
- `allow_concurrent_runs` manages task lifecycle
- `use_new_terminal` contrminal-based debuggers)
- Language extensions provide the "smart" task generation

---

## 4. Neovim: DAP + t.github.io/debug-adapter-protocol/overview
- https://microsoft.github.io/debug-adapter-protocol/specification

### Architecture: Composable Plugin Ecosystem

Neovim has NO built-in run/debug system. The ecosystem composes:
- **nvim-dap** — D (how to start/connect to debug adapter)
dap.adapters.python = {
  type = "executable",    -- or "server"
  command = "python",
  args = { "-m", "debugpy.adapter" },
  -- For "server" type:
  -- host = "127.0.0.1",
  -- port = 5678,
}

-- Configuration (what to debug)
dap.configurations.python = {
  {
    type = "python",          -- References adapter name
    request = "launch",       -- "launch" or "attach"
    name = "Launch file",
    program = "${file}",
    pythonPath = function()
      return "/usr/bin/python"
    end,
    args = function()
      return vim.split(vim.fn.input("Args: "), " ")
    end,
  },
}
```

**Key Dfn.getcwd(),
      env = { RUST_LOG = "info" },
      components = {
        { "on_output_quickfix", open = true },
        "default",
      },
    }
  end,
  condition = {
    filetype = { "rust" },
    -- Or: callback = function(searchestart logic, notifications, dependencies
- Built-in components: `on_output_quickfix`, `on_output_parse`, `restart_on_save`, `timeout`, `run_after`
- Users can define custom components

**Built-in task providers:**
- VS Code tasks.json (compatibility!)
- Make, npm, cargo, just, Makefile
- Shell scripts in project directory
- Directory-local tasks via `exrc`

### DAP + overseer Integration
- overseer supports `preLaunchTask` when used with nvim-dap
- Build task runs first, then debug session starts
- Mirrors VS Code's launch.json → tasks.json relationship

### Program Types Supported
- **Web**: Via debug adapters (Node.js, Chrome, Firefox)
- **CLI**: Any language with a DAP adapter
- **Native GUI**: C/C++ via codelldb, cppdbg; .NET via netcoredbg
- **Compile-only**: Via overseer tasks (no debug needed)
- **Embedded**: probe-rs, cortex-debug adapters
- **Remote**: Attach to remote processes via TCP

### Build System Decoupling
- Complete separation: overseer handles build, nvim-dap handles debug
- `preLaunchTask` bridges them
- overseer auto-detects tasks from build systems (Make, cargo, npm)
- Users can define arbitrary task templates

### Customization
- **Lua-native** — full programming language for configs
- Functions as field values (dynamic resolution)
- Component composition for task behavior
- Condition system for context-aware task availability
- Custom actions on tasks (restart, dispose, edit, etc.)

---

## Cross-Cutting Analysis

### Design Pattern Comparison

| Aspect | VS Code | JetBrains | Zed | Neovim |
|--------|---------|-----------|-----|--------|
| Config Format | JSON | XML (internal) | JSON | Lua |
| Type System | Extension-contributed `type` | Plugin-contributed templates | None (shell) | Adapter name |
| Build/Run Separation | launch.json vs tasks.json | Before Launch pipeline | None (all tasks) | overseer vs nvim-dap |
| Build Linkage | `preLaunchTask` (by label) | Before Launch list (ordered) | Shell composition | `preLaunchTask` |
| Dynamic Values | `${variable}`, `${input:}`, `${command:}` | Macros, path variables | `$ZED_*` env vars | Lua functions |
| Multi-target | Compound configurations | Compound type | Not supported | Manual orchestration |
| Sharing | `.vscode/` in VCS | `.idea/runConfigurations/` | `.zed/tasks.json` | Project-local lua |
| Auto-detection | Task providers (extensions) | Gutter icons, context menu | Language extensions | overseer providers |

### Key Abstraction: "request" Types

The DAP protocol (used by VS Code and nvim-dap) defines two fundamental modes:
1. **launch** — IDE starts the program
2. **attach** — IDE connects to already-running program

This is the minimal abstraction over "how to run" — everything else is type-specific.

### How They Handle Different Program Types

| Program Type | Launch Strategy | Output Handling | Lifecycle |
|--------------|----------------|-----------------|-----------|
| Web Server | Launch + keep running, open browser | Integrated terminal / output pane | Long-lived, manual stop |
| CLI Tool | Launch, wait for exit | Capture stdout/stderr | Short-lived, auto-terminate |
| Native GUI | Launch external process | External console / detach | Independent, may outlive IDE |
| Compile-only | Build task only, no launch | Problem matcher → diagnostics | Task completes |
| Embedded | Flash + attach via probe | Debug console (DAP) | Hardware-dependent |
| Tests | Launch test runner | Test results UI | Short-lived, report results |

### Design Principles Observed

1. **Separation of concerns**: Build is not run. Run is not debug. Each has its own config.
2. **Extension/plugin architecture**: Core defines the protocol, extensions provide types.
3. **Template + override**: Defaults come from templates/detection; users override specific fields.
4. **Variable substitution**: All systems provide context variables (current file, workspace, selection).
5. **Composability**: Complex workflows built from simple primitives (task chains, compounds).
6. **Sharing**: Configs stored in project for team sharing via VCS.
7. **Progressive complexity**: Simple cases work with zero config; complex cases have full control.
