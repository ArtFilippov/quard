//! Python syntax selection and reversible per-unit line maps.
use rustpython_ast::{Constant, Expr, Ranged, Stmt, Visitor};
use rustpython_parser::{ast, Parse};
use std::collections::{HashMap, HashSet};

pub struct Unit {
    pub text: String,
    pub lines: Vec<usize>,
    // Canonical schema columns map byte boundaries back to original columns.
    pub columns: Option<Vec<Vec<usize>>>,
    // A derived call unit covers one complete original subtree, not its tokens.
    pub subtree: Option<(usize, usize)>,
    // Token starts in prepared text whose callable identity survives renaming.
    pub identities: HashMap<usize, String>,
    // Prepared byte boundaries to original source offsets for folded control headers.
    pub positions: Option<Vec<Vec<usize>>>,
}

pub struct Views {
    pub behavior: Vec<Unit>,
    pub exact_behavior: Vec<Unit>,
    pub renamed_behavior: Vec<Unit>,
    pub schemas: Vec<Unit>,
    pub calls: Vec<Unit>,
}

// The exact detector receives one token per complete executable construct. A
// physical continuation of an expression cannot supply another eligible line,
// and a token match cannot start or end in the middle of a construct. The
// interning table is shared across files in a session (see lib.rs).
fn exact_constructs(
    text: &str,
    selection: &Selection,
    symbols: &mut HashMap<String, usize>,
) -> Vec<Unit> {
    let starts: Vec<usize> = std::iter::once(0)
        .chain(text.match_indices('\n').map(|(i, _)| i + 1))
        .collect();
    let mut result = Vec::new();
    for (kept, removed) in selection.keep.iter().zip(&selection.remove) {
        let mut constructs = Vec::new();
        for &(begin, end) in kept {
            // A compound statement owns its header; nested statements own their
            // own constructs. Do not count the whole compound a second time.
            let first_child = kept
                .iter()
                .filter(|(start, finish)| *start > begin && *finish <= end)
                .map(|(start, _)| *start)
                .min()
                .unwrap_or(end);
            let finish = first_child.min(end);
            if removed
                .iter()
                .any(|(start, stop)| *start <= begin && *stop >= finish)
            {
                continue;
            }
            let code = text[begin..finish].trim();
            if !code.is_empty() {
                constructs.push((begin, finish, code.to_owned()));
            }
        }
        constructs.sort_unstable_by_key(|(begin, _, _)| *begin);
        if constructs.is_empty() {
            continue;
        }
        let mut lines = Vec::new();
        let mut positions = Vec::new();
        let mut source = String::new();
        for (begin, end, code) in constructs {
            let line = starts.partition_point(|start| *start <= begin);
            let next = symbols.len();
            let symbol = *symbols.entry(code).or_insert(next);
            let row = format!("quardconstruct{symbol}");
            // The preparer only uses this token's equality. Its start and end
            // boundaries restore the original AST span, including continuations.
            let mut offsets = vec![begin; row.len() + 1];
            offsets[row.len()] = end;
            source.push_str(&row);
            source.push('\n');
            offsets.push(end);
            positions.push(offsets);
            lines.push(line);
        }
        result.push(Unit {
            text: source,
            lines,
            columns: None,
            subtree: None,
            identities: HashMap::new(),
            positions: Some(positions),
        });
    }
    result
}

#[derive(Default)]
pub struct ClassMembers {
    fields: HashSet<String>,
    properties: HashSet<String>,
}

pub type FieldIndex = HashMap<String, ClassMembers>;

struct StoredAssignments<'a> {
    receiver: &'a str,
    fields: HashSet<String>,
    rebound: bool,
}

impl Visitor for StoredAssignments<'_> {
    fn visit_stmt(&mut self, node: Stmt) {
        // Deferred bodies do not initialize this instance.
        if matches!(
            node,
            Stmt::FunctionDef(_) | Stmt::AsyncFunctionDef(_) | Stmt::ClassDef(_)
        ) {
            return;
        }
        self.generic_visit_stmt(node);
    }

    fn visit_expr(&mut self, node: Expr) {
        if let Expr::Name(name) = &node {
            if name.ctx == ast::ExprContext::Store && name.id.as_str() == self.receiver {
                self.rebound = true;
            }
        }
        if let Expr::Attribute(attr) = &node {
            if attr.ctx == ast::ExprContext::Store
                && matches!(attr.value.as_ref(), Expr::Name(name)
                    if name.id.as_str() == self.receiver)
            {
                self.fields.insert(attr.attr.to_string());
            }
        }
        self.generic_visit_expr(node);
    }
}

pub fn discover_fields(sources: &[(String, String)]) -> Result<FieldIndex, String> {
    let mut index = FieldIndex::new();
    let mut ambiguous = HashSet::new();
    for (path, text) in sources {
        let suite = ast::Suite::parse(text, path).map_err(|error| format!("{path}: {error}"))?;
        let names = CallIdentities::new(path, &index);
        let mut discovered = Vec::new();
        for stmt in suite {
            let Stmt::ClassDef(class) = stmt else {
                continue;
            };
            let mut members = ClassMembers::default();
            let mut opaque = !class.bases.is_empty() || !class.keywords.is_empty();
            for stmt in &class.body {
                match stmt {
                    Stmt::FunctionDef(method)
                        if method.name.as_str() == "__init__"
                            && method.decorator_list.is_empty() =>
                    {
                        members.fields.clear(); // Only the last direct constructor can run.
                        if let Some(receiver) = method
                            .args
                            .posonlyargs
                            .iter()
                            .chain(&method.args.args)
                            .next()
                            .map(|arg| arg.def.arg.as_str())
                        {
                            let mut assignments = StoredAssignments {
                                receiver,
                                fields: HashSet::new(),
                                rebound: false,
                            };
                            for stmt in &method.body {
                                assignments.visit_stmt(stmt.clone());
                            }
                            if !assignments.rebound {
                                members.fields.extend(assignments.fields);
                            }
                        }
                    }
                    Stmt::FunctionDef(method) => {
                        // A same-named method or opaque decorator can be a descriptor.
                        members.properties.insert(method.name.to_string());
                        if matches!(
                            method.name.as_str(),
                            "__getattribute__" | "__getattr__" | "__setattr__"
                        ) {
                            opaque = true;
                        }
                        if method.name.as_str() == "__init__" {
                            members.fields.clear();
                        }
                        for decorator in &method.decorator_list {
                            if let Expr::Attribute(attr) = decorator {
                                if matches!(attr.attr.as_str(), "getter" | "setter" | "deleter") {
                                    if let Expr::Name(name) = attr.value.as_ref() {
                                        members.properties.insert(name.id.to_string());
                                    }
                                }
                            }
                        }
                    }
                    Stmt::AsyncFunctionDef(method) => {
                        members.properties.insert(method.name.to_string());
                        if matches!(
                            method.name.as_str(),
                            "__getattribute__" | "__getattr__" | "__setattr__"
                        ) {
                            opaque = true;
                        }
                    }
                    Stmt::Assign(assignment) => {
                        for target in &assignment.targets {
                            if let Expr::Name(name) = target {
                                members.properties.insert(name.id.to_string());
                            }
                        }
                    }
                    Stmt::AnnAssign(assignment) => {
                        if let Expr::Name(name) = assignment.target.as_ref() {
                            if assignment.value.is_some() {
                                members.properties.insert(name.id.to_string());
                            }
                        }
                    }
                    _ => {}
                }
            }
            if opaque {
                members.fields.clear();
            }
            let class_name = names.declared(class.name.as_str());
            discovered.push((class_name, members));
        }
        for (name, members) in discovered {
            if index.contains_key(&name) {
                ambiguous.insert(name.clone());
            }
            index.insert(name, members);
        }
    }
    for name in ambiguous {
        if let Some(members) = index.get_mut(&name) {
            members.fields.clear();
        }
    }
    Ok(index)
}

// Bind only syntactically established callable names. Unknown calls keep their
// spelling; a local value binding never becomes a guessed callable identity.
struct CallIdentities<'a> {
    module: String,
    owner: String,
    bindings: HashMap<String, String>,
    receivers: HashMap<String, String>,
    fields: &'a FieldIndex,
    tokens: HashMap<usize, String>,
}

impl<'a> CallIdentities<'a> {
    fn new(path: &str, fields: &'a FieldIndex) -> Self {
        let relative = path
            .split("/src/")
            .nth(1)
            .or_else(|| path.split("/tests/").nth(1));
        let module = relative
            .unwrap_or(path)
            .trim_end_matches(".py")
            .replace('/', ".");
        let module = module.trim_end_matches(".__init__").to_owned();
        Self {
            module,
            owner: String::new(),
            bindings: HashMap::new(),
            receivers: HashMap::new(),
            fields,
            tokens: HashMap::new(),
        }
    }

    fn resolved(&self, expr: &Expr) -> Option<String> {
        match expr {
            Expr::Name(name) => self.bindings.get(name.id.as_str()).cloned(),
            Expr::Attribute(attr) => self
                .resolved(&attr.value)
                .map(|owner| format!("{owner}.{}", attr.attr)),
            _ => None,
        }
    }

    fn declared(&self, name: &str) -> String {
        if self.owner.is_empty() {
            format!("{}.{}", self.module, name)
        } else {
            format!("{}.{}.{}", self.module, self.owner, name)
        }
    }

    fn receiver_type(&self, expr: &Expr) -> Option<String> {
        match expr {
            Expr::Name(name) => self.receivers.get(name.id.as_str()).cloned(),
            Expr::Call(call) => self
                .resolved(&call.func)
                .filter(|name| self.fields.contains_key(name)),
            _ => None,
        }
    }

    fn method_receivers(&mut self, args: &ast::Arguments, owner: &str, implicit: bool) {
        let first = args.posonlyargs.iter().chain(&args.args).next();
        for arg in args
            .posonlyargs
            .iter()
            .chain(&args.args)
            .chain(&args.kwonlyargs)
        {
            self.receivers.remove(arg.def.arg.as_str());
            if let Some(annotation) = &arg.def.annotation {
                if let Some(name) = self
                    .resolved(annotation)
                    .filter(|name| self.fields.contains_key(name))
                {
                    self.receivers.insert(arg.def.arg.to_string(), name);
                }
            }
        }
        let class = format!("{}.{}", self.module, owner);
        if implicit && self.fields.contains_key(&class) {
            if let Some(arg) = first {
                self.receivers.insert(arg.def.arg.to_string(), class);
            }
        }
    }

    fn reference(&mut self, expr: &Expr, callable: bool) {
        match expr {
            Expr::Name(name) => {
                if let Some(identity) = self
                    .resolved(expr)
                    .or_else(|| callable.then(|| format!("unresolved:{}", name.id)))
                {
                    self.tokens.insert(name.range.start().into(), identity);
                }
            }
            Expr::Attribute(attr) if callable || self.resolved(expr).is_some() => {
                let identity = self
                    .resolved(expr)
                    .unwrap_or_else(|| format!("method:{}", attr.attr));
                let end: usize = attr.range.end().into();
                self.tokens.insert(end - attr.attr.len(), identity);
            }
            Expr::Attribute(attr) => {
                let owner = match attr.value.as_ref() {
                    Expr::Name(name) => self.receivers.get(name.id.as_str()),
                    _ => None,
                };
                let role = if owner.is_some_and(|name| {
                    self.fields.get(name).is_some_and(|members| {
                        members.fields.contains(attr.attr.as_str())
                            && !members.properties.contains(attr.attr.as_str())
                    })
                }) {
                    "field"
                } else {
                    "attribute"
                };
                let end: usize = attr.range.end().into();
                self.tokens
                    .entry(end - attr.attr.len())
                    .or_insert_with(|| format!("{role}:{}", attr.attr));
            }
            _ => {}
        }
    }

    fn forget(&mut self, expr: &Expr) {
        match expr {
            Expr::Name(name) => {
                self.bindings.remove(name.id.as_str());
                self.receivers.remove(name.id.as_str());
            }
            Expr::Tuple(items) => {
                for item in &items.elts {
                    self.forget(item);
                }
            }
            Expr::List(items) => {
                for item in &items.elts {
                    self.forget(item);
                }
            }
            _ => {}
        }
    }

    fn arguments(&mut self, args: &ast::Arguments) {
        for arg in args
            .posonlyargs
            .iter()
            .chain(&args.args)
            .chain(&args.kwonlyargs)
        {
            self.bindings.remove(arg.def.arg.as_str());
        }
        for arg in [&args.vararg, &args.kwarg].into_iter().flatten() {
            self.bindings.remove(arg.arg.as_str());
        }
    }

    fn body(&mut self, body: &[Stmt]) {
        for stmt in body {
            self.visit_stmt(stmt.clone());
        }
    }

    fn branches(&mut self, paths: &[Vec<Stmt>]) {
        let before = self.bindings.clone();
        let before_receivers = self.receivers.clone();
        let mut states = Vec::new();
        let mut receiver_states = Vec::new();
        for path in paths {
            self.bindings = before.clone();
            self.receivers = before_receivers.clone();
            self.body(path);
            states.push(self.bindings.clone());
            receiver_states.push(self.receivers.clone());
        }
        self.bindings = before;
        self.receivers = before_receivers;
        self.bindings
            .retain(|name, identity| states.iter().all(|state| state.get(name) == Some(identity)));
        // A binding assigned on every path can also be proven.
        if let Some(first) = states.first() {
            for (name, identity) in first {
                if states.iter().all(|state| state.get(name) == Some(identity)) {
                    self.bindings.insert(name.clone(), identity.clone());
                }
            }
        }
        self.receivers.retain(|name, class| {
            receiver_states
                .iter()
                .all(|state| state.get(name) == Some(class))
        });
        if let Some(first) = receiver_states.first() {
            for (name, class) in first {
                if receiver_states
                    .iter()
                    .all(|state| state.get(name) == Some(class))
                {
                    self.receivers.insert(name.clone(), class.clone());
                }
            }
        }
    }
}

impl Visitor for CallIdentities<'_> {
    fn visit_stmt(&mut self, node: Stmt) {
        match node {
            Stmt::Import(def) => {
                for alias in def.names {
                    let name = alias.asname.as_ref().map_or_else(
                        || {
                            alias
                                .name
                                .as_str()
                                .split('.')
                                .next()
                                .unwrap_or("")
                                .to_owned()
                        },
                        ToString::to_string,
                    );
                    let module = if alias.asname.is_some() {
                        alias.name.to_string()
                    } else {
                        name.clone()
                    };
                    self.receivers.remove(&name);
                    self.bindings.insert(name, module);
                }
            }
            Stmt::ImportFrom(def) => {
                let level = def.level.map_or(0, |value| value.to_u32() as usize);
                let mut parts: Vec<&str> = self.module.split('.').collect();
                if level > 0 {
                    parts.truncate(parts.len().saturating_sub(level));
                } else {
                    parts.clear();
                }
                let prefix = parts.join(".");
                let module = def.module.as_ref().map_or("", |value| value.as_str());
                for alias in def.names {
                    if alias.name.as_str() == "*" {
                        continue;
                    }
                    let binding = alias.asname.as_ref().unwrap_or(&alias.name).to_string();
                    let target = [prefix.as_str(), module, alias.name.as_str()]
                        .into_iter()
                        .filter(|part| !part.is_empty())
                        .collect::<Vec<_>>()
                        .join(".");
                    self.receivers.remove(&binding);
                    self.bindings.insert(binding, target);
                }
            }
            Stmt::FunctionDef(def) => {
                let parent = self.bindings.clone();
                let receivers = self.receivers.clone();
                let declared = self.declared(def.name.as_str());
                let owner = self.owner.clone();
                self.owner = declared[self.module.len() + 1..].to_owned();
                self.arguments(&def.args);
                self.method_receivers(&def.args, &owner, def.decorator_list.is_empty());
                self.bindings.insert(def.name.to_string(), declared.clone());
                self.body(&def.body);
                self.bindings = parent;
                self.receivers = receivers;
                self.owner = owner;
                self.receivers.remove(def.name.as_str());
                self.bindings.insert(def.name.to_string(), declared);
            }
            Stmt::AsyncFunctionDef(def) => {
                let parent = self.bindings.clone();
                let receivers = self.receivers.clone();
                let declared = self.declared(def.name.as_str());
                let owner = self.owner.clone();
                self.owner = declared[self.module.len() + 1..].to_owned();
                self.arguments(&def.args);
                self.method_receivers(&def.args, &owner, def.decorator_list.is_empty());
                self.bindings.insert(def.name.to_string(), declared.clone());
                self.body(&def.body);
                self.bindings = parent;
                self.receivers = receivers;
                self.owner = owner;
                self.receivers.remove(def.name.as_str());
                self.bindings.insert(def.name.to_string(), declared);
            }
            Stmt::ClassDef(def) => {
                let parent = self.bindings.clone();
                let receivers = self.receivers.clone();
                self.receivers.clear();
                let declared = self.declared(def.name.as_str());
                let owner = self.owner.clone();
                self.owner = declared[self.module.len() + 1..].to_owned();
                self.body(&def.body);
                self.bindings = parent;
                self.receivers = receivers;
                self.owner = owner;
                self.receivers.remove(def.name.as_str());
                self.bindings.insert(def.name.to_string(), declared);
            }
            Stmt::Assign(def) => {
                self.visit_expr(*def.value.clone());
                let identity = self.resolved(&def.value);
                let receiver = self.receiver_type(&def.value);
                for target in &def.targets {
                    self.forget(target);
                    if let (Expr::Name(name), Some(identity)) = (target, &identity) {
                        self.bindings.insert(name.id.to_string(), identity.clone());
                    }
                    if let (Expr::Name(name), Some(receiver)) = (target, &receiver) {
                        self.receivers.insert(name.id.to_string(), receiver.clone());
                    }
                }
            }
            Stmt::AnnAssign(def) => {
                if let Some(value) = &def.value {
                    self.visit_expr(*value.clone());
                }
                let identity = def.value.as_ref().and_then(|value| self.resolved(value));
                let receiver = if let Some(value) = &def.value {
                    self.receiver_type(value)
                } else {
                    self.resolved(&def.annotation)
                        .filter(|name| self.fields.contains_key(name))
                };
                self.forget(&def.target);
                if let (Expr::Name(name), Some(identity)) = (def.target.as_ref(), identity) {
                    self.bindings.insert(name.id.to_string(), identity);
                }
                if let (Expr::Name(name), Some(receiver)) = (def.target.as_ref(), receiver) {
                    self.receivers.insert(name.id.to_string(), receiver);
                }
            }
            Stmt::If(def) => {
                self.visit_expr(*def.test);
                self.branches(&[def.body, def.orelse]);
            }
            Stmt::For(def) => {
                self.visit_expr(*def.iter);
                self.forget(&def.target);
                self.branches(&[def.body, def.orelse, Vec::new()]);
            }
            Stmt::AsyncFor(def) => {
                self.visit_expr(*def.iter);
                self.forget(&def.target);
                self.branches(&[def.body, def.orelse, Vec::new()]);
            }
            Stmt::While(def) => {
                self.visit_expr(*def.test);
                self.branches(&[def.body, def.orelse, Vec::new()]);
            }
            Stmt::AugAssign(def) => {
                self.visit_expr(*def.value);
                self.forget(&def.target);
            }
            Stmt::Delete(def) => {
                for target in def.targets {
                    self.forget(&target);
                }
            }
            other => {
                let before = self.bindings.clone();
                let before_receivers = self.receivers.clone();
                self.generic_visit_stmt(other);
                // Other compound statements may not execute their whole body.
                // Keep only identities that survived every possible path.
                self.bindings
                    .retain(|name, identity| before.get(name) == Some(identity));
                self.receivers
                    .retain(|name, class| before_receivers.get(name) == Some(class));
            }
        }
    }

    fn visit_expr(&mut self, node: Expr) {
        if let Expr::NamedExpr(binding) = &node {
            self.forget(&binding.target);
        }
        if let Expr::Call(call) = &node {
            self.reference(&call.func, true);
            for keyword in &call.keywords {
                if let Some(name) = &keyword.arg {
                    let start: usize = keyword.range.start().into();
                    self.tokens.insert(start, format!("keyword:{name}"));
                }
            }
        } else {
            self.reference(&node, false);
        }
        self.generic_visit_expr(node);
    }
}

// The normalized pass needs a candidate for a nested expression even when
// each call starts on a different line and the surrounding constructor differs.
// Make each *complete* three-call subtree a native unit. In particular, do not
// slide a token window over the literal leaves or closing parentheses.
struct NestedCalls<'a> {
    source: &'a str,
    chain_length: usize,
    path: Vec<(usize, usize, String, HashMap<usize, String>)>,
    units: Vec<Unit>,
    literal_continuations: Vec<(usize, usize)>,
    identities: &'a HashMap<usize, String>,
}

impl Visitor for NestedCalls<'_> {
    fn visit_stmt(&mut self, node: Stmt) {
        match node {
            Stmt::FunctionDef(def) => {
                for stmt in def.body {
                    self.visit_stmt(stmt);
                }
            }
            Stmt::AsyncFunctionDef(def) => {
                for stmt in def.body {
                    self.visit_stmt(stmt);
                }
            }
            Stmt::ClassDef(def) => {
                for stmt in def.body {
                    self.visit_stmt(stmt);
                }
            }
            Stmt::Import(_) | Stmt::ImportFrom(_) | Stmt::TypeAlias(_) => {}
            Stmt::AnnAssign(def) if def.value.is_none() => {}
            other => self.generic_visit_stmt(other),
        }
    }

    fn visit_expr(&mut self, node: Expr) {
        let literal = match &node {
            Expr::Constant(value) => matches!(value.value, Constant::Str(_)),
            Expr::List(value) => value
                .elts
                .iter()
                .all(|item| matches!(item, Expr::Constant(_))),
            Expr::Set(value) => value
                .elts
                .iter()
                .all(|item| matches!(item, Expr::Constant(_))),
            Expr::Tuple(value) => value
                .elts
                .iter()
                .all(|item| matches!(item, Expr::Constant(_))),
            Expr::Dict(value) => value
                .keys
                .iter()
                .flatten()
                .chain(&value.values)
                .all(|item| matches!(item, Expr::Constant(_))),
            _ => false,
        };
        if literal {
            let start: usize = node.range().start().into();
            let end: usize = node.range().end().into();
            if let Some(newline) = self.source[start..end].find('\n') {
                self.literal_continuations.push((start + newline + 1, end));
            }
        }
        if let Expr::Call(call) = &node {
            let start: usize = node.range().start().into();
            let end: usize = node.range().end().into();
            if matches!(call.func.as_ref(), Expr::Name(_) | Expr::Attribute(_)) {
                let children: Vec<_> = call
                    .args
                    .iter()
                    .chain(call.keywords.iter().map(|kw| &kw.value))
                    .filter(|arg| matches!(arg, Expr::Call(_)))
                    .collect();
                let mut row = String::new();
                let mut identities = HashMap::new();
                let mut cursor = start;
                let mut spans: Vec<_> = children.iter().map(|child| child.range()).collect();
                spans.sort_by_key(|span| span.start());
                for span in spans {
                    let begin: usize = span.start().into();
                    let finish: usize = span.end().into();
                    for (&offset, identity) in self.identities {
                        if (cursor..begin).contains(&offset) {
                            identities.insert(row.len() + offset - cursor, identity.clone());
                        }
                    }
                    row.push_str(&self.source[cursor..begin]);
                    row.push_str("child");
                    cursor = finish;
                }
                for (&offset, identity) in self.identities {
                    if (cursor..end).contains(&offset) {
                        identities.insert(row.len() + offset - cursor, identity.clone());
                    }
                }
                row.push_str(&self.source[cursor..end]);
                self.path
                    .push((start, end, row.replace('\n', " "), identities));
                if children.is_empty() && self.path.len() >= self.chain_length {
                    let chain = &self.path[self.path.len() - self.chain_length..];
                    let lines: Vec<usize> = chain
                        .iter()
                        .map(|(start, _, _, _)| {
                            self.source[..*start]
                                .bytes()
                                .filter(|byte| *byte == b'\n')
                                .count()
                                + 1
                        })
                        .collect();
                    if lines.windows(2).all(|pair| pair[0] < pair[1]) {
                        let mut text = String::new();
                        let mut protected = HashMap::new();
                        for (_, _, row, identities) in chain {
                            for (offset, identity) in identities {
                                protected.insert(text.len() + offset, identity.clone());
                            }
                            text.push_str(row);
                            text.push('\n');
                        }
                        self.units.push(Unit {
                            text,
                            lines,
                            columns: None,
                            subtree: Some((chain[0].0, chain[0].1)),
                            identities: protected,
                            positions: None,
                        });
                    }
                }
                for child in call
                    .args
                    .iter()
                    .chain(call.keywords.iter().map(|kw| &kw.value))
                {
                    self.visit_expr(child.clone());
                }
                self.path.pop();
                return;
            }
        }
        self.generic_visit_expr(node);
    }
}

#[derive(Default)]
struct Selection {
    keep: Vec<Vec<(usize, usize)>>,
    remove: Vec<Vec<(usize, usize)>>,
    current: usize,
    attribute_docs: bool,
    local_imports: bool,
    alias_names: HashSet<String>,
    typing_modules: HashSet<String>,
    typed_dict_names: HashSet<String>,
    typed_dict_modules: HashSet<String>,
    protocol_names: HashSet<String>,
    protocol_modules: HashSet<String>,
    abc_names: HashSet<String>,
    abc_meta_names: HashSet<String>,
    abc_modules: HashSet<String>,
    abstract_names: HashSet<String>,
    contracts: HashSet<String>,
    schemas: Vec<Vec<(usize, usize)>>,
    canonical_schemas: Vec<Unit>,
    declarations: Vec<Unit>,
    text: String,
    error: Option<String>,
}

struct Field {
    name: String,
    name_span: (usize, usize),
    annotation: (usize, usize),
    line: usize,
}

impl Selection {
    fn forget_binding(&mut self, binding: &str) {
        self.alias_names.remove(binding);
        self.typing_modules.remove(binding);
        self.typed_dict_names.remove(binding);
        self.typed_dict_modules.remove(binding);
        self.protocol_names.remove(binding);
        self.protocol_modules.remove(binding);
        self.abc_names.remove(binding);
        self.abc_meta_names.remove(binding);
        self.abc_modules.remove(binding);
        self.abstract_names.remove(binding);
        self.contracts.remove(binding);
    }

    fn base_name(base: &Expr) -> &Expr {
        if let Expr::Subscript(subscript) = base {
            &subscript.value
        } else {
            base
        }
    }

    fn imported_name(
        base: &Expr,
        names: &HashSet<String>,
        modules: &HashSet<String>,
        member: &str,
    ) -> bool {
        match Self::base_name(base) {
            Expr::Name(name) => names.contains(name.id.as_str()),
            Expr::Attribute(attr) => {
                attr.attr.as_str() == member
                    && matches!(attr.value.as_ref(), Expr::Name(name)
                    if modules.contains(name.id.as_str()))
            }
            _ => false,
        }
    }

    fn contract_class(&self, def: &ast::StmtClassDef) -> bool {
        if self.protocol_class(def) {
            return true;
        }
        let abc = def.bases.iter().any(|base| {
            Self::imported_name(base, &self.abc_names, &self.abc_modules, "ABC")
                || matches!(base, Expr::Name(name)
                    if self.contracts.contains(name.id.as_str()))
        }) || def.keywords.iter().any(|kw| {
            kw.arg
                .as_ref()
                .is_some_and(|arg| arg.as_str() == "metaclass")
                && Self::imported_name(
                    &kw.value,
                    &self.abc_meta_names,
                    &self.abc_modules,
                    "ABCMeta",
                )
        });
        abc && def.body.iter().any(|stmt| match stmt {
            Stmt::FunctionDef(method) => self.abstract_decorators(&method.decorator_list),
            Stmt::AsyncFunctionDef(method) => self.abstract_decorators(&method.decorator_list),
            _ => false,
        })
    }

    fn protocol_class(&self, def: &ast::StmtClassDef) -> bool {
        def.bases.iter().any(|base| {
            Self::imported_name(
                base,
                &self.protocol_names,
                &self.protocol_modules,
                "Protocol",
            )
        })
    }

    fn abstract_decorators(&self, decorators: &[Expr]) -> bool {
        decorators.iter().any(|dec| {
            Self::imported_name(
                dec,
                &self.abstract_names,
                &self.abc_modules,
                "abstractmethod",
            ) || Self::imported_name(
                dec,
                &self.abstract_names,
                &self.abc_modules,
                "abstractproperty",
            )
        })
    }

    fn contract_methods(&mut self, body: &[Stmt]) {
        let mut text = String::new();
        let mut lines = Vec::new();
        let mut columns = Vec::new();
        for stmt in body {
            let (name, statements, decorators) = match stmt {
                Stmt::FunctionDef(def) => (def.name.as_str(), &def.body, &def.decorator_list),
                Stmt::AsyncFunctionDef(def) => (def.name.as_str(), &def.body, &def.decorator_list),
                _ => continue,
            };
            if name.starts_with("__") && name.ends_with("__") {
                continue;
            }
            let property = decorators.iter().find_map(|decorator| match decorator {
                Expr::Name(value)
                    if matches!(value.id.as_str(), "property" | "abstractproperty") =>
                {
                    Some((name.to_owned(), "getter"))
                }
                Expr::Attribute(value)
                    if matches!(value.attr.as_str(), "property" | "abstractproperty") =>
                {
                    Some((name.to_owned(), "getter"))
                }
                Expr::Attribute(value)
                    if matches!(value.attr.as_str(), "getter" | "setter" | "deleter") =>
                {
                    match value.value.as_ref() {
                        Expr::Name(owner) => Some((owner.id.to_string(), value.attr.as_str())),
                        _ => None,
                    }
                }
                _ => None,
            });
            // Cached properties describe behavior, not accessor declarations.
            if decorators.iter().any(|decorator| match decorator {
                Expr::Name(value) => value.id.as_str() == "cached_property",
                Expr::Attribute(value) => value.attr.as_str() == "cached_property",
                _ => false,
            }) {
                continue;
            }
            let header_body_start: usize = statements
                .first()
                .map_or(0, |first| first.range().start().into());
            let statements = if matches!(statements.first(), Some(Stmt::Expr(expr))
                if matches!(expr.value.as_ref(), Expr::Constant(value)
                    if matches!(value.value, Constant::Str(_))))
            {
                &statements[1..]
            } else {
                statements.as_slice()
            };
            if statements.len() != 1 || !Self::placeholder(&statements[0]) {
                continue;
            }
            let start: usize = stmt.range().start().into();
            let body_start: usize = statements[0].range().start().into();
            let body_end: usize = statements[0].range().end().into();
            let signature_line = self.text[..start].bytes().filter(|b| *b == b'\n').count() + 1;
            let placeholder_line = self.text[..body_start]
                .bytes()
                .filter(|b| *b == b'\n')
                .count()
                + 1;
            if self.text[body_start..body_end].contains('\n') {
                continue;
            }
            let body_column = body_start - self.text[..body_start].rfind('\n').map_or(0, |i| i + 1);
            let placeholder = &self.text[body_start..body_end];
            let signature = self.text[start..header_body_start].trim_end();
            if !signature.ends_with(':') {
                continue;
            }
            let signature_end = start + signature.len();
            let identity = property.map_or_else(String::new, |(owner, kind)| {
                format!("property {owner} {kind} ")
            });
            let declaration = format!("{identity}{signature}");
            let marker = |row: usize| {
                format!(
                    "{} ",
                    serde_json::to_string(&format!("{declaration} line {row}"))
                        .expect("signature is valid UTF-8")
                )
            };
            let mut cursor = start;
            let mut row = 0;
            while cursor < signature_end {
                let end = self.text[cursor..signature_end]
                    .find('\n')
                    .map_or(signature_end, |index| cursor + index);
                let part = &self.text[cursor..end];
                let original_column = cursor - self.text[..cursor].rfind('\n').map_or(0, |i| i + 1);
                // Each original line remains eligible, but its exact contract
                // signature and row must agree before it can match. Distinct
                // signatures cannot share a long continuation or placeholder.
                let prefix = marker(row);
                let mut canonical = format!("{prefix}{part}");
                let mut map = vec![original_column; prefix.len()];
                map.extend((0..=part.len()).map(|i| original_column + i));
                if end == signature_end
                    && placeholder_line
                        == self.text[..end].bytes().filter(|b| *b == b'\n').count() + 1
                {
                    canonical.push(' ');
                    canonical.push_str(placeholder);
                    map.push(body_column);
                    map.extend((1..=placeholder.len()).map(|i| body_column + i));
                }
                text.push_str(&canonical);
                text.push('\n');
                lines.push(self.text[..cursor].bytes().filter(|b| *b == b'\n').count() + 1);
                columns.push(map);
                cursor = end + 1;
                row += 1;
            }
            if placeholder_line == signature_line
                || self.text[..signature_end]
                    .bytes()
                    .filter(|b| *b == b'\n')
                    .count()
                    + 1
                    == placeholder_line
            {
                continue;
            }
            // Tie the placeholder to its declaration: repeated pass/ellipsis alone
            // must never match methods whose names or signatures differ.
            let synthetic = format!("{}{placeholder}", marker(row));
            let mut map = vec![body_column; synthetic.len() - placeholder.len() + 1];
            map.extend((1..=placeholder.len()).map(|i| body_column + i));
            text.push_str(&synthetic);
            text.push('\n');
            lines.push(placeholder_line);
            columns.push(map);
        }
        if !lines.is_empty() {
            self.declarations.push(Unit {
                text,
                lines,
                columns: Some(columns),
                subtree: None,
                identities: HashMap::new(),
                positions: None,
            });
        }
    }

    fn placeholder(stmt: &Stmt) -> bool {
        match stmt {
            Stmt::Pass(_) => true,
            Stmt::Expr(expr) => matches!(expr.value.as_ref(), Expr::Constant(value)
                if matches!(value.value, Constant::Ellipsis)),
            Stmt::Raise(raised) if raised.cause.is_none() => match raised.exc.as_deref() {
                Some(Expr::Name(name)) => name.id.as_str() == "NotImplementedError",
                Some(Expr::Call(call)) => {
                    matches!(call.func.as_ref(), Expr::Name(name)
                            if name.id.as_str() == "NotImplementedError")
                        && call.keywords.is_empty()
                        && call.args.iter().all(|arg| matches!(arg, Expr::Constant(_)))
                }
                _ => false,
            },
            _ => false,
        }
    }

    fn is_type_alias(&self, annotation: &Expr) -> bool {
        match annotation {
            Expr::Name(name) => self.alias_names.contains(name.id.as_str()),
            Expr::Attribute(attr) => {
                matches!(attr.value.as_ref(), Expr::Name(name)
                if self.typing_modules.contains(name.id.as_str()))
                    && attr.attr.as_str() == "TypeAlias"
            }
            _ => false,
        }
    }

    fn import_bindings(&mut self, stmt: &Stmt) {
        match stmt {
            Stmt::Import(node) => {
                for alias in &node.names {
                    let module = alias.name.as_str();
                    let binding = alias.asname.as_ref().map_or_else(
                        || module.split('.').next().unwrap_or(module),
                        |name| name.as_str(),
                    );
                    self.forget_binding(binding);
                    if matches!(module, "typing" | "typing_extensions") {
                        self.typing_modules.insert(binding.to_owned());
                        self.typed_dict_modules.insert(binding.to_owned());
                        self.protocol_modules.insert(binding.to_owned());
                    } else if module == "abc" {
                        self.abc_modules.insert(binding.to_owned());
                    }
                }
            }
            Stmt::ImportFrom(node) => {
                for alias in &node.names {
                    let binding = alias.asname.as_ref().unwrap_or(&alias.name).as_str();
                    self.forget_binding(binding);
                    if node.level.is_none_or(|level| level.to_u32() == 0)
                        && node.module.as_ref().is_some_and(|module| {
                            matches!(module.as_str(), "typing" | "typing_extensions")
                        })
                        && alias.name.as_str() == "TypeAlias"
                    {
                        self.alias_names.insert(binding.to_owned());
                    }
                    if node.level.is_none_or(|level| level.to_u32() == 0)
                        && node.module.as_ref().is_some_and(|module| {
                            matches!(module.as_str(), "typing" | "typing_extensions")
                        })
                        && alias.name.as_str() == "TypedDict"
                    {
                        self.typed_dict_names.insert(binding.to_owned());
                    }
                    if node.level.is_none_or(|level| level.to_u32() == 0) {
                        match (
                            node.module.as_ref().map(|m| m.as_str()),
                            alias.name.as_str(),
                        ) {
                            (Some("typing" | "typing_extensions"), "Protocol") => {
                                self.protocol_names.insert(binding.to_owned());
                            }
                            (Some("abc"), "ABC") => {
                                self.abc_names.insert(binding.to_owned());
                            }
                            (Some("abc"), "ABCMeta") => {
                                self.abc_meta_names.insert(binding.to_owned());
                            }
                            (Some("abc"), "abstractmethod" | "abstractproperty") => {
                                self.abstract_names.insert(binding.to_owned());
                            }
                            _ => {}
                        }
                    }
                }
            }
            _ => {}
        }
    }

    fn forget_target(&mut self, target: &Expr) {
        match target {
            Expr::Name(name) => {
                self.forget_binding(name.id.as_str());
            }
            Expr::Tuple(items) => {
                for item in &items.elts {
                    self.forget_target(item);
                }
            }
            Expr::List(items) => {
                for item in &items.elts {
                    self.forget_target(item);
                }
            }
            Expr::Starred(item) => self.forget_target(&item.value),
            _ => {}
        }
    }

    fn forget_arguments(&mut self, args: &ast::Arguments) {
        for argument in args
            .posonlyargs
            .iter()
            .chain(&args.args)
            .chain(&args.kwonlyargs)
        {
            self.forget_binding(argument.def.arg.as_str());
        }
        for argument in [&args.vararg, &args.kwarg].into_iter().flatten() {
            self.forget_binding(argument.arg.as_str());
        }
    }

    fn is_typed_dict(&self, base: &Expr) -> bool {
        match base {
            Expr::Name(name) => self.typed_dict_names.contains(name.id.as_str()),
            Expr::Attribute(attr) => {
                attr.attr.as_str() == "TypedDict"
                    && matches!(attr.value.as_ref(), Expr::Name(name)
                    if self.typed_dict_modules.contains(name.id.as_str()))
            }
            _ => false,
        }
    }

    fn field(
        &self,
        name: String,
        name_span: (usize, usize),
        annotation: (usize, usize),
        functional: bool,
    ) -> Option<Field> {
        let text = &self.text;
        let start = text[..name_span.0].rfind('\n').map_or(0, |i| i + 1);
        let end = text[annotation.1..]
            .find('\n')
            .map_or(text.len(), |i| annotation.1 + i);
        if annotation.1 > end
            || text[name_span.0..annotation.1].contains('\n')
            || !text[start..name_span.0].trim().is_empty()
        {
            return None;
        }
        if functional {
            let suffix = text[annotation.1..end]
                .split('#')
                .next()
                .unwrap_or("")
                .trim();
            if !matches!(suffix, "" | ",") {
                return None;
            }
        }
        Some(Field {
            name,
            name_span,
            annotation,
            line: text[..start].bytes().filter(|byte| *byte == b'\n').count() + 1,
        })
    }

    fn canonical_schema(&mut self, fields: &[Field]) {
        if fields.is_empty() || fields.windows(2).any(|pair| pair[0].line >= pair[1].line) {
            return;
        }
        let mut text = String::new();
        let mut lines = Vec::new();
        let mut columns = Vec::new();
        for field in fields {
            let original_start = self.text[..field.name_span.0]
                .rfind('\n')
                .map_or(0, |i| i + 1);
            let name_start = field.name_span.0 - original_start;
            let name_end = field.name_span.1 - original_start;
            let type_start = field.annotation.0 - original_start;
            let type_end = field.annotation.1 - original_start;
            let mut map = vec![name_start; 5]; // Four uniform indentation bytes.
            let mut canonical = format!("    {}: ", field.name);
            // Original literal keys include quotes; only mapped boundaries are
            // exposed to the reporter, never the canonical spelling.
            for offset in 1..=field.name.len() {
                map.push(if offset == field.name.len() {
                    name_end
                } else {
                    (name_start + offset).min(name_end)
                });
            }
            map.push(name_end);
            map.push(type_start);
            let annotation = &self.text[field.annotation.0..field.annotation.1];
            canonical.push_str(annotation);
            for offset in 1..=annotation.len() {
                map.push(type_start + offset);
            }
            if map.len() != canonical.len() + 1 || map.last() != Some(&type_end) {
                self.error = Some("schema field column map is incomplete".to_owned());
                return;
            }
            text.push_str(&canonical);
            text.push('\n');
            columns.push(map);
            lines.push(field.line);
        }
        self.canonical_schemas.push(Unit {
            text,
            lines,
            columns: Some(columns),
            subtree: None,
            identities: HashMap::new(),
            positions: None,
        });
    }

    fn functional_schema(&mut self, call: &ast::ExprCall) {
        if !self.is_typed_dict(&call.func)
            || call.args.is_empty()
            || !matches!(&call.args[0], Expr::Constant(value) if matches!(value.value, Constant::Str(_)))
        {
            return;
        }
        let mut fields = Vec::new();
        if call.args.len() == 2
            && call
                .keywords
                .iter()
                .all(|kw| kw.arg.as_ref().is_some_and(|n| n.as_str() == "total"))
        {
            let Expr::Dict(dictionary) = &call.args[1] else {
                return;
            };
            for (key, value) in dictionary.keys.iter().zip(&dictionary.values) {
                let Some(Expr::Constant(key)) = key else {
                    return;
                };
                let Constant::Str(name) = &key.value else {
                    return;
                };
                let key_range = key.range();
                let span = (key_range.start().into(), key_range.end().into());
                let literal = &self.text[span.0..span.1];
                if !((literal.starts_with('\'') && literal.ends_with('\''))
                    || (literal.starts_with('"') && literal.ends_with('"')))
                {
                    return;
                }
                let range = value.range();
                let annotation = (range.start().into(), range.end().into());
                let identifier = matches!(Expr::parse(name, "<schema-field>"), Ok(Expr::Name(_)));
                let canonical_name = if identifier {
                    name.clone()
                } else {
                    serde_json::to_string(name).expect("string field name")
                };
                let Some(field) = self.field(canonical_name, span, annotation, true) else {
                    return;
                };
                fields.push(field);
            }
        } else if call.args.len() == 1 {
            for kw in &call.keywords {
                let Some(name) = &kw.arg else { return };
                if name.as_str() == "total" {
                    continue;
                }
                let range = kw.range();
                let value = kw.value.range();
                let annotation = (value.start().into(), value.end().into());
                let start: usize = range.start().into();
                let span = (start, start + name.len());
                let Some(field) = self.field(name.to_string(), span, annotation, true) else {
                    return;
                };
                fields.push(field);
            }
        } else {
            return;
        }
        self.canonical_schema(&fields);
    }

    fn schema_fields(&mut self, body: &[Stmt]) {
        let mut run = Vec::new();
        let mut fields = Vec::new();
        for stmt in body {
            if let Stmt::AnnAssign(field) = stmt {
                if field.value.is_none()
                    && matches!(field.target.as_ref(), Expr::Name(_))
                    && !self.is_type_alias(&field.annotation)
                {
                    let range = stmt.range();
                    run.push((range.start().into(), range.end().into()));
                    if let Expr::Name(name) = field.target.as_ref() {
                        let name_range = name.range();
                        let annotation = field.annotation.range();
                        if let Some(item) = self.field(
                            name.id.to_string(),
                            (name_range.start().into(), name_range.end().into()),
                            (annotation.start().into(), annotation.end().into()),
                            false,
                        ) {
                            fields.push(item);
                        }
                    }
                    continue;
                }
            }
            if !run.is_empty() {
                self.finish_schema(&mut run, &mut fields);
            }
        }
        if !run.is_empty() {
            self.finish_schema(&mut run, &mut fields);
        }
    }

    fn finish_schema(&mut self, run: &mut Vec<(usize, usize)>, fields: &mut Vec<Field>) {
        if run.len() == fields.len() && fields.windows(2).all(|pair| pair[0].line < pair[1].line) {
            self.canonical_schema(fields);
        } else {
            self.schemas.push(std::mem::take(run));
        }
        run.clear();
        fields.clear();
    }

    fn suite(&mut self, body: Vec<Stmt>) {
        let mut previous_assignment = false;
        for (index, stmt) in body.into_iter().enumerate() {
            let doc = matches!(&stmt, Stmt::Expr(expr) if matches!(expr.value.as_ref(),
                Expr::Constant(value) if matches!(value.value, Constant::Str(_))));
            let assignment = matches!(&stmt, Stmt::Assign(_) | Stmt::AnnAssign(_));
            if doc && (index == 0 || self.attribute_docs && previous_assignment) {
                let range = stmt.range();
                self.remove[self.current].push((range.start().into(), range.end().into()));
            } else if Self::placeholder(&stmt) {
                let range = stmt.range();
                self.remove[self.current].push((range.start().into(), range.end().into()));
            } else {
                self.visit_stmt(stmt);
            }
            previous_assignment = assignment;
        }
    }

    fn unit(
        &mut self,
        body: Vec<Stmt>,
        range: (usize, usize),
        attribute_docs: bool,
        args: Option<&ast::Arguments>,
    ) {
        for unit in &mut self.remove {
            unit.push(range);
        }
        let parent = self.current;
        let parent_docs = self.attribute_docs;
        let parent_imports = self.local_imports;
        let parent_aliases = self.alias_names.clone();
        let parent_modules = self.typing_modules.clone();
        let parent_dict_names = self.typed_dict_names.clone();
        let parent_dict_modules = self.typed_dict_modules.clone();
        let parent_protocol_names = self.protocol_names.clone();
        let parent_protocol_modules = self.protocol_modules.clone();
        let parent_abc_names = self.abc_names.clone();
        let parent_abc_meta_names = self.abc_meta_names.clone();
        let parent_abc_modules = self.abc_modules.clone();
        let parent_abstract_names = self.abstract_names.clone();
        let parent_contracts = self.contracts.clone();
        self.keep.push(Vec::new());
        self.remove.push(Vec::new());
        self.current = self.keep.len() - 1;
        self.attribute_docs = attribute_docs;
        self.local_imports = !attribute_docs;
        if let Some(args) = args {
            self.forget_arguments(args);
        }
        self.suite(body);
        self.current = parent;
        self.attribute_docs = parent_docs;
        self.local_imports = parent_imports;
        self.alias_names = parent_aliases;
        self.typing_modules = parent_modules;
        self.typed_dict_names = parent_dict_names;
        self.typed_dict_modules = parent_dict_modules;
        self.protocol_names = parent_protocol_names;
        self.protocol_modules = parent_protocol_modules;
        self.abc_names = parent_abc_names;
        self.abc_meta_names = parent_abc_meta_names;
        self.abc_modules = parent_abc_modules;
        self.abstract_names = parent_abstract_names;
        self.contracts = parent_contracts;
    }
}

impl Visitor for Selection {
    fn visit_stmt(&mut self, node: Stmt) {
        let range = node.range();
        let span = (range.start().into(), range.end().into());
        match node {
            Stmt::FunctionDef(def) => {
                self.unit(def.body, span, false, Some(&def.args));
                self.forget_binding(def.name.as_str());
            }
            Stmt::AsyncFunctionDef(def) => {
                self.unit(def.body, span, false, Some(&def.args));
                self.forget_binding(def.name.as_str());
            }
            Stmt::ClassDef(def) => {
                let contract = self.contract_class(&def);
                if contract {
                    self.contract_methods(&def.body);
                }
                if def.bases.iter().any(|base| self.is_typed_dict(base)) {
                    self.schema_fields(&def.body);
                }
                self.unit(def.body, span, true, None);
                self.forget_binding(def.name.as_str());
                if contract {
                    self.contracts.insert(def.name.to_string());
                }
            }
            Stmt::Import(_) | Stmt::ImportFrom(_) => {
                self.import_bindings(&node);
                if self.local_imports {
                    self.keep[self.current].push(span);
                } else {
                    self.remove[self.current].push(span);
                }
            }
            Stmt::TypeAlias(def) => {
                self.remove[self.current].push(span);
                self.forget_target(&def.name);
            }
            Stmt::AnnAssign(def) if def.value.is_none() || self.is_type_alias(&def.annotation) => {
                self.remove[self.current].push(span);
                self.forget_target(&def.target);
            }
            Stmt::AnnAssign(def) => {
                self.keep[self.current].push(span);
                self.forget_target(&def.target);
            }
            Stmt::Assign(def) => {
                let mut schema = false;
                if def.targets.len() == 1 && matches!(&def.targets[0], Expr::Name(_)) {
                    if let Expr::Call(call) = def.value.as_ref() {
                        if self.is_typed_dict(&call.func) {
                            self.functional_schema(call);
                            schema = true;
                        }
                    }
                }
                if schema {
                    self.remove[self.current].push(span);
                } else {
                    self.keep[self.current].push(span);
                }
                for target in &def.targets {
                    self.forget_target(target);
                }
            }
            other => {
                self.keep[self.current].push(span);
                // Nested definitions in conditional suites have separate ownership.
                self.generic_visit_stmt(other);
            }
        }
    }
}

// Decide what survives before building the normalized native candidates. Headers
// are comparison context for a surviving body, never behavioral lines on their own.
struct RenamedSelection<'a> {
    text: &'a str,
    identities: &'a HashMap<usize, String>,
    remove: Vec<(usize, usize)>,
    headers: HashSet<usize>,
    contexts: HashMap<usize, Vec<(usize, String)>>,
}

impl RenamedSelection<'_> {
    fn value(&self, expr: &Expr) -> bool {
        match expr {
            Expr::Name(_) | Expr::Constant(_) => true,
            Expr::Attribute(attr) => {
                let end: usize = attr.range.end().into();
                self.value(&attr.value)
                    && self
                        .identities
                        .get(&(end - attr.attr.len()))
                        .is_some_and(|role| role.starts_with("field:"))
            }
            Expr::Tuple(items) => items.elts.iter().all(|item| self.value(item)),
            Expr::List(items) => items.elts.iter().all(|item| self.value(item)),
            Expr::Set(items) => items.elts.iter().all(|item| self.value(item)),
            Expr::Dict(items) => items.keys.iter().zip(&items.values).all(|(key, value)| {
                key.as_ref().is_some_and(|key| self.value(key)) && self.value(value)
            }),
            _ => false,
        }
    }

    fn binding(expr: &Expr) -> bool {
        match expr {
            Expr::Name(_) => true,
            Expr::Tuple(items) => items.elts.iter().all(Self::binding),
            Expr::List(items) => items.elts.iter().all(Self::binding),
            Expr::Starred(item) => Self::binding(&item.value),
            _ => false,
        }
    }

    fn body(&mut self, body: &[Stmt]) -> bool {
        let mut surviving = false;
        for stmt in body {
            surviving |= self.statement(stmt);
        }
        surviving
    }

    fn control(&mut self, stmt: &Stmt, bodies: &[&[Stmt]]) -> bool {
        // Do not short-circuit: every branch must be pruned before selection.
        let mut surviving = false;
        for body in bodies {
            surviving |= self.body(body);
        }
        let range = stmt.range();
        let start: usize = range.start().into();
        let end: usize = range.end().into();
        let mut children: Vec<(usize, usize)> = bodies
            .iter()
            .flat_map(|body| body.iter())
            .map(|child| (child.range().start().into(), child.range().end().into()))
            .collect();
        children.sort_unstable();
        if surviving {
            let mut cursor = self.text[..start].rfind('\n').map_or(0, |i| i + 1);
            while cursor < end {
                let finish = self.text[cursor..]
                    .find('\n')
                    .map_or(self.text.len(), |i| cursor + i);
                let row = &self.text[cursor..finish];
                let header = row.split('#').next().unwrap_or("").trim_end();
                if header.ends_with(':')
                    && !children.iter().any(|(begin, finish)| {
                        *begin < cursor + row.len()
                            && *finish > cursor
                            && self.text[..*begin].rfind('\n') == self.text[..cursor].rfind('\n')
                    })
                {
                    self.headers.insert(cursor);
                }
                cursor = finish + 1;
            }
            let header = self
                .headers
                .iter()
                .filter(|row| **row >= start && **row < end)
                .min()
                .copied()
                .unwrap_or(start);
            let marker = self.text[header..]
                .split_whitespace()
                .next()
                .unwrap_or("")
                .trim_end_matches(':')
                .to_owned();
            let mut row = self.text[..start].rfind('\n').map_or(0, |i| i + 1);
            while row < end {
                if row > header {
                    self.contexts
                        .entry(row)
                        .or_default()
                        .push((header, marker.clone()));
                }
                row = self.text[row..]
                    .find('\n')
                    .map_or(self.text.len(), |i| row + i + 1);
            }
        } else {
            // Mask syntax around children, not a nested definition's independent
            // unit. Its own body can still contain behavioral statements.
            let mut cursor = start;
            for (begin, finish) in children {
                if begin > cursor {
                    self.remove.push((cursor, begin));
                }
                cursor = cursor.max(finish);
            }
            if cursor < end {
                self.remove.push((cursor, end));
            }
        }
        surviving
    }

    fn statement(&mut self, stmt: &Stmt) -> bool {
        let simple = match stmt {
            Stmt::Assign(def) => def.targets.iter().all(Self::binding) && self.value(&def.value),
            Stmt::AnnAssign(def) => {
                Self::binding(&def.target)
                    && def.value.as_ref().is_none_or(|value| self.value(value))
            }
            Stmt::Return(def) => def.value.as_ref().is_none_or(|value| self.value(value)),
            Stmt::Expr(def) => matches!(def.value.as_ref(), Expr::Constant(value)
                if matches!(value.value, Constant::Str(_) | Constant::Ellipsis)),
            Stmt::Pass(_) => true,
            Stmt::FunctionDef(def) => {
                self.body(&def.body);
                return false;
            }
            Stmt::AsyncFunctionDef(def) => {
                self.body(&def.body);
                return false;
            }
            Stmt::ClassDef(def) => {
                self.body(&def.body);
                return false;
            }
            Stmt::If(def) => return self.control(stmt, &[&def.body, &def.orelse]),
            Stmt::For(def) => return self.control(stmt, &[&def.body, &def.orelse]),
            Stmt::AsyncFor(def) => return self.control(stmt, &[&def.body, &def.orelse]),
            Stmt::While(def) => return self.control(stmt, &[&def.body, &def.orelse]),
            Stmt::With(def) => return self.control(stmt, &[&def.body]),
            Stmt::AsyncWith(def) => return self.control(stmt, &[&def.body]),
            Stmt::Match(def) => {
                let bodies: Vec<&[Stmt]> =
                    def.cases.iter().map(|case| case.body.as_slice()).collect();
                return self.control(stmt, &bodies);
            }
            Stmt::Try(def) => {
                let mut bodies = vec![
                    def.body.as_slice(),
                    def.orelse.as_slice(),
                    def.finalbody.as_slice(),
                ];
                for handler in &def.handlers {
                    let ast::ExceptHandler::ExceptHandler(handler) = handler;
                    bodies.push(&handler.body);
                }
                return self.control(stmt, &bodies);
            }
            Stmt::TryStar(def) => {
                let mut bodies = vec![
                    def.body.as_slice(),
                    def.orelse.as_slice(),
                    def.finalbody.as_slice(),
                ];
                for handler in &def.handlers {
                    let ast::ExceptHandler::ExceptHandler(handler) = handler;
                    bodies.push(&handler.body);
                }
                return self.control(stmt, &bodies);
            }
            _ => false,
        };
        if simple {
            let range = stmt.range();
            self.remove.push((range.start().into(), range.end().into()));
        }
        !simple
    }
}

fn compact_units(
    text: &str,
    path: &str,
    starts: &[usize],
    spans: impl Iterator<Item = (Vec<(usize, usize)>, Vec<(usize, usize)>)>,
    normalized: bool,
    extra_remove: &[(usize, usize)],
    identities: &HashMap<usize, String>,
    headers: &HashSet<usize>,
    contexts: &HashMap<usize, Vec<(usize, String)>>,
) -> Result<Vec<Unit>, String> {
    let mut result = Vec::new();
    let mut anchors: Vec<_> = identities.iter().collect();
    anchors.sort_unstable_by_key(|(offset, _)| **offset);
    for (keep, remove) in spans {
        let mut bytes = vec![b' '; text.len()];
        for (begin, end) in keep {
            bytes[begin..end].copy_from_slice(&text.as_bytes()[begin..end]);
        }
        for (begin, end) in remove {
            bytes[begin..end].fill(b' ');
        }
        if normalized {
            for &(begin, end) in extra_remove {
                bytes[begin..end].fill(b' ');
            }
        }
        let mut lines = Vec::new();
        let mut compact = String::new();
        let mut protected = HashMap::new();
        let mut positions = Vec::new();
        let mut pending = Vec::new();
        for (index, start) in starts.iter().enumerate() {
            let end = starts.get(index + 1).copied().unwrap_or(text.len());
            let slice = &bytes[*start..end];
            if normalized {
                let trimmed = slice
                    .iter()
                    .copied()
                    .filter(|byte| !byte.is_ascii_whitespace());
                if trimmed.clone().next().is_some()
                    && trimmed
                        .clone()
                        .all(|byte| matches!(byte, b')' | b']' | b'}' | b','))
                {
                    continue;
                }
            }
            if slice.iter().any(|b| !b.is_ascii_whitespace()) {
                if normalized && headers.contains(start) {
                    pending.extend((*start..end).filter(|offset| bytes[*offset] != b'\n'));
                    continue;
                }
                let mut row = Vec::new();
                if normalized {
                    if let Some(context) = contexts.get(start) {
                        let mut context = context.clone();
                        context.sort_unstable_by_key(|(offset, _)| *offset);
                        for (offset, marker) in &context {
                            let token = "quardcontext";
                            protected.insert(compact.len(), format!("control:{marker}"));
                            compact.push_str(token);
                            row.extend(std::iter::repeat_n(*offset, token.len()));
                            compact.push(' ');
                            row.push(*offset);
                        }
                    }
                }
                if normalized && !pending.is_empty() {
                    let prefix: Vec<u8> = pending.iter().map(|offset| bytes[*offset]).collect();
                    for (index, offset) in pending.iter().enumerate() {
                        if headers.contains(offset) {
                            let keyword_start = prefix[index..]
                                .iter()
                                .position(|byte| !byte.is_ascii_whitespace())
                                .map(|shift| index + shift);
                            if let Some(begin) = keyword_start {
                                let end = begin
                                    + prefix[begin..]
                                        .iter()
                                        .take_while(|byte| byte.is_ascii_alphabetic())
                                        .count();
                                if end > begin {
                                    protected.insert(
                                        compact.len() + begin,
                                        format!(
                                            "control:{}",
                                            String::from_utf8_lossy(&prefix[begin..end])
                                        ),
                                    );
                                }
                            }
                        }
                    }
                    compact.push_str(
                        std::str::from_utf8(&prefix).map_err(|_| {
                            format!("{path}: folded header split a Unicode character")
                        })?,
                    );
                    row.append(&mut pending);
                    compact.push(' ');
                    row.push(*start);
                }
                if normalized {
                    let first = anchors.partition_point(|(offset, _)| **offset < *start);
                    for (&offset, identity) in &anchors[first..] {
                        if offset >= end {
                            break;
                        }
                        if bytes[offset] != b' ' {
                            protected.insert(compact.len() + offset - start, (*identity).clone());
                        }
                    }
                }
                let masked = std::str::from_utf8(slice)
                    .map_err(|_| format!("{path}: masked syntax split a Unicode character"))?;
                compact.push_str(masked.trim_end_matches('\n'));
                compact.push('\n');
                if normalized {
                    row.extend((*start..end).filter(|offset| bytes[*offset] != b'\n'));
                    row.push(end.saturating_sub(1));
                    positions.push(row);
                }
                lines.push(index + 1);
            }
        }
        if !lines.is_empty() {
            result.push(Unit {
                text: compact,
                lines,
                columns: None,
                subtree: None,
                identities: protected,
                positions: normalized.then_some(positions),
            });
        }
    }
    Ok(result)
}

pub fn prepare(
    text: &str,
    path: &str,
    min_lines: usize,
    fields: &FieldIndex,
    exact_symbols: &mut HashMap<String, usize>,
) -> Result<Views, String> {
    let suite = ast::Suite::parse(text, path).map_err(|error| format!("{path}: {error}"))?;
    let mut identity = CallIdentities::new(path, fields);
    identity.body(&suite);
    let mut calls = NestedCalls {
        source: text,
        chain_length: min_lines.max(3),
        path: Vec::new(),
        units: Vec::new(),
        literal_continuations: Vec::new(),
        identities: &identity.tokens,
    };
    for stmt in &suite {
        calls.visit_stmt(stmt.clone());
    }
    let mut selection = Selection {
        keep: vec![Vec::new()],
        remove: vec![Vec::new()],
        attribute_docs: true,
        text: text.to_owned(),
        ..Selection::default()
    };
    let starts: Vec<usize> = std::iter::once(0)
        .chain(text.match_indices('\n').map(|(i, _)| i + 1))
        .collect();
    let mut renamed = RenamedSelection {
        text,
        identities: &identity.tokens,
        remove: Vec::new(),
        headers: HashSet::new(),
        contexts: HashMap::new(),
    };
    for stmt in &suite {
        renamed.statement(stmt);
    }
    selection.suite(suite);
    if let Some(error) = selection.error.take() {
        return Err(format!("{path}: {error}"));
    }
    let exact_behavior = exact_constructs(text, &selection, exact_symbols);
    Ok(Views {
        exact_behavior,
        renamed_behavior: compact_units(
            text,
            path,
            &starts,
            selection
                .keep
                .iter()
                .cloned()
                .zip(selection.remove.iter().cloned()),
            true,
            &[
                calls.literal_continuations.as_slice(),
                renamed.remove.as_slice(),
            ]
            .concat(),
            &identity.tokens,
            &renamed.headers,
            &renamed.contexts,
        )?,
        behavior: compact_units(
            text,
            path,
            &starts,
            selection.keep.into_iter().zip(selection.remove),
            false,
            &[],
            &identity.tokens,
            &HashSet::new(),
            &HashMap::new(),
        )?,
        schemas: compact_units(
            text,
            path,
            &starts,
            selection.schemas.into_iter().map(|run| (run, Vec::new())),
            false,
            &[],
            &identity.tokens,
            &HashSet::new(),
            &HashMap::new(),
        )?
        .into_iter()
        .chain(selection.canonical_schemas)
        .chain(selection.declarations)
        .collect(),
        calls: calls.units,
    })
}
