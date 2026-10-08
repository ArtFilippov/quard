use std::collections::HashMap;
use std::fs;
use std::path::Path;

fn main() {
    println!("cargo:rerun-if-changed=Cargo.lock");
    let lock = fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("Cargo.lock"))
        .expect("the wheel requires a checked-in Cargo.lock");
    let mut versions = HashMap::new();
    for package in lock.split("[[package]]").skip(1) {
        let name = package.lines().find_map(|line| {
            line.strip_prefix("name = \"")
                .and_then(|value| value.strip_suffix('"'))
        });
        let version = package.lines().find_map(|line| {
            line.strip_prefix("version = \"")
                .and_then(|value| value.strip_suffix('"'))
        });
        if let (Some(name), Some(version)) = (name, version) {
            if name.starts_with("cpd-") && versions.insert(name, version).is_some() {
                panic!("multiple linked versions of {name}");
            }
        }
    }
    for (package, variable) in [
        ("cpd-core", "QUARD_CPD_CORE_VERSION"),
        ("cpd-finder", "QUARD_CPD_FINDER_VERSION"),
        ("cpd-reporter", "QUARD_CPD_REPORTER_VERSION"),
        ("cpd-tokenizer", "QUARD_CPD_TOKENIZER_VERSION"),
    ] {
        let version = versions
            .get(package)
            .unwrap_or_else(|| panic!("{package} missing from lock"));
        println!("cargo:rustc-env={variable}={version}");
    }
}
