// SPDX-License-Identifier: MPL-2.0
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::cmp::Ordering;
use std::collections::{BTreeMap, HashMap};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use uuid::Uuid;

const MAX_SCAN_DEPTH: usize = 5;
const MAX_ENTRIES_EXAMINED: usize = 2_000;
const MAX_SOURCE_FILES: usize = 64;
const MAX_TOTAL_SOURCE_BYTES: u64 = 40 * 1024 * 1024;
const MAX_STRUCTURED_SOURCE_BYTES: u64 = 10 * 1024 * 1024;
const MAX_TEXT_SOURCE_BYTES: u64 = 2 * 1024 * 1024;
const MAX_CANONICAL_PROFILE_BYTES: u64 = 4 * 1024 * 1024;
const CANONICAL_DISPLAY_NAME_LIMIT: usize = 160;

#[cfg(windows)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct WindowsFileIdentity {
    volume_serial_number: u32,
    file_index: u64,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct StagedSource {
    pub ordinal: usize,
    pub managed_relative_path: String,
    pub display_name: String,
    pub format: String,
    pub byte_size: u64,
    pub checksum_sha256: String,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct SourceScanCounts {
    pub discovered: usize,
    pub staged: usize,
    pub skipped: usize,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct StagedSourceFolder {
    pub scan_id: String,
    pub sources: Vec<StagedSource>,
    pub file_counts: SourceScanCounts,
    pub scan_issues: BTreeMap<String, usize>,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub(crate) struct StagedCanonicalProfile {
    pub scan_id: String,
    pub managed_relative_path: String,
    pub display_name: String,
    pub raw_sha256: String,
    pub raw_bytes: u64,
}

#[derive(Debug)]
struct CandidateSource {
    path: PathBuf,
    canonical_path: PathBuf,
    display_name: String,
    format: &'static str,
    suffix: &'static str,
    byte_size: u64,
    #[cfg(unix)]
    device: u64,
    #[cfg(unix)]
    inode: u64,
    #[cfg(windows)]
    windows_identity: WindowsFileIdentity,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CandidateRejection {
    Empty,
    ManagedSource,
    NonRegular,
    Symlink,
    TooLarge,
    Unsupported,
}

impl CandidateRejection {
    fn issue_code(self) -> &'static str {
        match self {
            Self::Empty => "empty_file",
            Self::ManagedSource => "managed_folder_skipped",
            Self::NonRegular => "non_regular_file_skipped",
            Self::Symlink => "symlink_skipped",
            Self::TooLarge => "file_too_large",
            Self::Unsupported => "unsupported_file_type",
        }
    }

    fn selected_file_error(self) -> &'static str {
        match self {
            Self::Empty => "source_file_empty: A selected source file is empty",
            Self::ManagedSource => {
                "source_file_managed: A selected source file is inside the managed local vault"
            }
            Self::NonRegular => "source_file_non_regular: A selected source is not a regular file",
            Self::Symlink => "source_file_symlink: A selected source is a symbolic link",
            Self::TooLarge => {
                "source_file_size_limit: A selected source file exceeds its per-file size limit"
            }
            Self::Unsupported => {
                "source_file_type_unsupported: A selected source file type is not supported"
            }
        }
    }
}

fn safe_scan_id(value: &str) -> Result<String, String> {
    let parsed = Uuid::parse_str(value).map_err(|_| "The source scan ID is invalid".to_string())?;
    let normalized = parsed.to_string();
    if normalized != value {
        return Err("The source scan ID is invalid".to_string());
    }
    Ok(normalized)
}

fn format_for_path(path: &Path) -> Option<(&'static str, &'static str)> {
    let extension = path.extension()?.to_str()?.to_ascii_lowercase();
    match extension.as_str() {
        "pdf" => Some(("pdf", "pdf")),
        "docx" => Some(("docx", "docx")),
        "json" => Some(("json", "json")),
        "md" | "markdown" => Some(("md", "md")),
        "txt" => Some(("txt", "txt")),
        "csv" => Some(("csv", "csv")),
        _ => None,
    }
}

fn max_bytes_for_format(format: &str) -> u64 {
    if matches!(format, "pdf" | "docx") {
        MAX_STRUCTURED_SOURCE_BYTES
    } else {
        MAX_TEXT_SOURCE_BYTES
    }
}

fn should_skip_directory(name: &str) -> bool {
    name.starts_with('.')
        || matches!(
            name.to_ascii_lowercase().as_str(),
            "node_modules" | "target" | "__pycache__" | "venv" | ".venv"
        )
}

fn safe_display_name(relative: &Path) -> String {
    let candidate = relative
        .components()
        .filter_map(|component| match component {
            Component::Normal(value) => value.to_str(),
            _ => None,
        })
        .collect::<Vec<_>>()
        .join("/");
    let cleaned: String = candidate
        .chars()
        .filter(|character| !character.is_control())
        .take(240)
        .collect();
    if cleaned.trim().is_empty() {
        "source-document".to_string()
    } else {
        cleaned
    }
}

enum CandidateInspection {
    Candidate(CandidateSource),
    Rejected(CandidateRejection),
}

fn metadata_matches(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    if !right.is_file() || left.len() != right.len() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        left.dev() == right.dev() && left.ino() == right.ino()
    }
    #[cfg(not(unix))]
    {
        true
    }
}

#[cfg(windows)]
fn windows_file_identity(file: &File) -> Result<WindowsFileIdentity, String> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        BY_HANDLE_FILE_INFORMATION, FILE_ATTRIBUTE_REPARSE_POINT, GetFileInformationByHandle,
    };

    let mut information = BY_HANDLE_FILE_INFORMATION::default();
    // SAFETY: `file` owns a live Windows handle for the duration of this call,
    // and `information` is a valid writable output buffer of the required type.
    let result =
        unsafe { GetFileInformationByHandle(file.as_raw_handle().cast(), &mut information) };
    if result == 0 {
        return Err("A selected source file could not be verified".to_string());
    }
    if information.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(
            "source_file_symlink: A selected source uses a Windows reparse point".to_string(),
        );
    }
    Ok(WindowsFileIdentity {
        volume_serial_number: information.dwVolumeSerialNumber,
        file_index: (u64::from(information.nFileIndexHigh) << 32)
            | u64::from(information.nFileIndexLow),
    })
}

fn inspect_candidate(
    path: &Path,
    display_path: &Path,
    data_root: &Path,
) -> Result<CandidateInspection, String> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|_| "A selected source file could not be inspected".to_string())?;
    if metadata.file_type().is_symlink() {
        return Ok(CandidateInspection::Rejected(CandidateRejection::Symlink));
    }
    if !metadata.file_type().is_file() {
        return Ok(CandidateInspection::Rejected(
            CandidateRejection::NonRegular,
        ));
    }
    let Some((format, suffix)) = format_for_path(path) else {
        return Ok(CandidateInspection::Rejected(
            CandidateRejection::Unsupported,
        ));
    };
    let byte_size = metadata.len();
    if byte_size == 0 {
        return Ok(CandidateInspection::Rejected(CandidateRejection::Empty));
    }
    if byte_size > max_bytes_for_format(format) {
        return Ok(CandidateInspection::Rejected(CandidateRejection::TooLarge));
    }

    let canonical_path = fs::canonicalize(path)
        .map_err(|_| "A selected source file could not be verified".to_string())?;
    if canonical_path.starts_with(data_root) {
        return Ok(CandidateInspection::Rejected(
            CandidateRejection::ManagedSource,
        ));
    }
    let verified_metadata = fs::symlink_metadata(path)
        .map_err(|_| "A selected source file became unavailable".to_string())?;
    if verified_metadata.file_type().is_symlink()
        || !metadata_matches(&metadata, &verified_metadata)
    {
        return Err("A selected source changed while it was being staged".to_string());
    }

    #[cfg(windows)]
    let windows_identity = {
        let source = open_source_file(path)?;
        let opened_metadata = source
            .metadata()
            .map_err(|_| "A selected source file could not be inspected".to_string())?;
        if !opened_metadata.is_file() || opened_metadata.len() != byte_size {
            return Err("A selected source changed while it was being staged".to_string());
        }
        let identity = windows_file_identity(&source)?;
        let opened_canonical_path = fs::canonicalize(path)
            .map_err(|_| "A selected source file could not be verified".to_string())?;
        if opened_canonical_path != canonical_path {
            return Err("A selected source changed while it was being staged".to_string());
        }
        identity
    };

    Ok(CandidateInspection::Candidate(CandidateSource {
        path: path.to_path_buf(),
        canonical_path,
        display_name: safe_display_name(display_path),
        format,
        suffix,
        byte_size,
        #[cfg(unix)]
        device: {
            use std::os::unix::fs::MetadataExt;
            metadata.dev()
        },
        #[cfg(unix)]
        inode: {
            use std::os::unix::fs::MetadataExt;
            metadata.ino()
        },
        #[cfg(windows)]
        windows_identity,
    }))
}

fn compare_candidates(left: &CandidateSource, right: &CandidateSource) -> Ordering {
    left.display_name
        .to_lowercase()
        .cmp(&right.display_name.to_lowercase())
        .then_with(|| left.display_name.cmp(&right.display_name))
        .then_with(|| left.canonical_path.cmp(&right.canonical_path))
}

fn same_selected_file(left: &CandidateSource, right: &CandidateSource) -> bool {
    #[cfg(unix)]
    {
        left.device == right.device && left.inode == right.inode
    }
    #[cfg(windows)]
    {
        left.windows_identity == right.windows_identity
    }
    #[cfg(not(any(unix, windows)))]
    {
        left.canonical_path == right.canonical_path
    }
}

fn secure_directory_permissions(_path: &Path) -> Result<(), String> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(_path, fs::Permissions::from_mode(0o700))
            .map_err(|_| "Unable to secure source staging".to_string())?;
    }
    Ok(())
}

fn managed_directory_chain(
    data_dir: &Path,
    components: &[&str],
    create_missing: bool,
) -> Result<Option<PathBuf>, String> {
    let data_metadata = fs::symlink_metadata(data_dir)
        .map_err(|_| "Unable to inspect the local data directory".to_string())?;
    if data_metadata.file_type().is_symlink() || !data_metadata.is_dir() {
        return Err("The local data directory is unsafe".to_string());
    }
    let canonical_root = fs::canonicalize(data_dir)
        .map_err(|_| "Unable to verify the local data directory".to_string())?;
    let mut current = data_dir.to_path_buf();
    for component in components {
        current.push(component);
        match fs::symlink_metadata(&current) {
            Ok(metadata) => {
                if metadata.file_type().is_symlink() || !metadata.is_dir() {
                    return Err("The source staging path has an unsafe ancestor".to_string());
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound && create_missing => {
                fs::create_dir(&current)
                    .map_err(|_| "Unable to create secure source staging".to_string())?;
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(_) => return Err("Unable to inspect source staging".to_string()),
        }
        let canonical_current = fs::canonicalize(&current)
            .map_err(|_| "Unable to verify source staging".to_string())?;
        if !canonical_current.starts_with(&canonical_root) {
            return Err("The source staging path escaped the local vault".to_string());
        }
        secure_directory_permissions(&current)?;
    }
    Ok(Some(current))
}

fn open_staging_file(path: &Path) -> Result<File, String> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options
        .open(path)
        .map_err(|_| "Unable to create a staged source snapshot".to_string())
}

#[cfg(unix)]
fn open_source_file(path: &Path) -> Result<File, String> {
    use rustix::fs::{Mode, OFlags, open};

    let descriptor = open(path, OFlags::RDONLY | OFlags::NOFOLLOW, Mode::empty())
        .map_err(|_| "A selected source file could not be opened".to_string())?;
    Ok(File::from(descriptor))
}

#[cfg(windows)]
fn open_source_file(path: &Path) -> Result<File, String> {
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_FLAG_OPEN_REPARSE_POINT, FILE_FLAG_SEQUENTIAL_SCAN, FILE_SHARE_READ,
    };

    OpenOptions::new()
        .read(true)
        // Readers may coexist, but a writer or delete/rename request cannot be
        // granted while the selected bytes are inspected or copied.
        .share_mode(FILE_SHARE_READ)
        // Open the final path component itself instead of following a reparse
        // point. The handle attributes are checked immediately after opening.
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN)
        .open(path)
        .map_err(|_| "A selected source file could not be opened safely".to_string())
}

#[cfg(not(any(unix, windows)))]
fn open_source_file(_path: &Path) -> Result<File, String> {
    Err("Direct source-file selection is unavailable on this platform".to_string())
}

fn copy_candidate(candidate: &CandidateSource, target: &Path) -> Result<(u64, String), String> {
    let link_metadata = fs::symlink_metadata(&candidate.path)
        .map_err(|_| "A selected source file became unavailable".to_string())?;
    if link_metadata.file_type().is_symlink() || !link_metadata.file_type().is_file() {
        return Err("A selected source is not a regular file".to_string());
    }
    if link_metadata.len() != candidate.byte_size {
        return Err("A selected source changed while it was being staged".to_string());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if link_metadata.dev() != candidate.device || link_metadata.ino() != candidate.inode {
            return Err("A selected source changed while it was being staged".to_string());
        }
    }

    let mut source = open_source_file(&candidate.path)?;
    #[cfg(windows)]
    if windows_file_identity(&source)? != candidate.windows_identity {
        return Err("A selected source changed while it was being staged".to_string());
    }
    let opened_metadata = source
        .metadata()
        .map_err(|_| "A selected source file could not be inspected".to_string())?;
    if !opened_metadata.is_file() || opened_metadata.len() != candidate.byte_size {
        return Err("A selected source changed while it was being staged".to_string());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if opened_metadata.dev() != candidate.device || opened_metadata.ino() != candidate.inode {
            return Err("A selected source changed while it was being staged".to_string());
        }
    }

    let mut destination = open_staging_file(target)?;
    let mut digest = Sha256::new();
    let mut total = 0_u64;
    let maximum = max_bytes_for_format(candidate.format);
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = source
            .read(&mut buffer)
            .map_err(|_| "A selected source file could not be read".to_string())?;
        if read == 0 {
            break;
        }
        total = total.saturating_add(read as u64);
        if total > maximum || total > candidate.byte_size {
            return Err("A selected source changed while it was being staged".to_string());
        }
        digest.update(&buffer[..read]);
        destination
            .write_all(&buffer[..read])
            .map_err(|_| "A source snapshot could not be staged".to_string())?;
    }
    if total != candidate.byte_size {
        return Err("A selected source changed while it was being staged".to_string());
    }
    destination
        .sync_all()
        .map_err(|_| "A source snapshot could not be finalized".to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(target, fs::Permissions::from_mode(0o600))
            .map_err(|_| "A source snapshot could not be secured".to_string())?;
    }
    Ok((total, format!("{:x}", digest.finalize())))
}

fn canonical_display_name(path: &Path) -> String {
    let cleaned: String = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("canonical-profile.json")
        .chars()
        .filter(|character| !character.is_control())
        .take(CANONICAL_DISPLAY_NAME_LIMIT)
        .collect();
    if cleaned.trim().is_empty() {
        "canonical-profile.json".to_string()
    } else {
        cleaned
    }
}

fn staging_directory(data_dir: &Path, scan_id: &str) -> Result<PathBuf, String> {
    let normalized = safe_scan_id(scan_id)?;
    Ok(data_dir.join("imports").join("staging").join(normalized))
}

pub(crate) fn cleanup_staging(data_dir: &Path, scan_id: &str) -> Result<(), String> {
    let target = staging_directory(data_dir, scan_id)?;
    let Some(expected_parent) = managed_directory_chain(data_dir, &["imports", "staging"], false)?
    else {
        return Ok(());
    };
    if target.parent() != Some(expected_parent.as_path()) {
        return Err("The source staging path is invalid".to_string());
    }
    match fs::symlink_metadata(&target) {
        Ok(metadata) => {
            if metadata.file_type().is_symlink() || !metadata.is_dir() {
                return Err("The source staging path is unsafe".to_string());
            }
            fs::remove_dir_all(&target)
                .map_err(|_| "Unable to remove source staging".to_string())?;
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(_) => return Err("Unable to inspect source staging".to_string()),
    }
    Ok(())
}

pub(crate) fn stage_canonical_profile(
    selected_file: &Path,
    data_dir: &Path,
    scan_id: &str,
) -> Result<StagedCanonicalProfile, String> {
    let normalized_scan_id = safe_scan_id(scan_id)?;
    let link_metadata = fs::symlink_metadata(selected_file)
        .map_err(|_| "The selected profile file could not be inspected".to_string())?;
    if link_metadata.file_type().is_symlink() || !link_metadata.file_type().is_file() {
        return Err("The selected profile must be a regular file".to_string());
    }
    if link_metadata.len() > MAX_CANONICAL_PROFILE_BYTES {
        return Err("The selected profile file exceeds the 4 MiB import limit".to_string());
    }

    let data_root = fs::canonicalize(data_dir)
        .map_err(|_| "Unable to verify the local data directory".to_string())?;
    let canonical_path = fs::canonicalize(selected_file)
        .map_err(|_| "The selected profile file could not be verified".to_string())?;
    if canonical_path.starts_with(&data_root) {
        return Err("The selected profile is already inside the managed local vault".to_string());
    }

    let mut source = open_source_file(selected_file)?;
    #[cfg(windows)]
    windows_file_identity(&source)?;
    let opened_metadata = source
        .metadata()
        .map_err(|_| "The selected profile file could not be inspected".to_string())?;
    if !metadata_matches(&link_metadata, &opened_metadata)
        || opened_metadata.len() > MAX_CANONICAL_PROFILE_BYTES
    {
        return Err("The selected profile changed while it was being staged".to_string());
    }
    let opened_canonical_path = fs::canonicalize(selected_file)
        .map_err(|_| "The selected profile file could not be verified".to_string())?;
    if opened_canonical_path != canonical_path {
        return Err("The selected profile changed while it was being staged".to_string());
    }

    let mut bytes = Vec::with_capacity(opened_metadata.len() as usize);
    (&mut source)
        .take(MAX_CANONICAL_PROFILE_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "The selected profile file could not be read".to_string())?;
    if bytes.len() as u64 > MAX_CANONICAL_PROFILE_BYTES {
        return Err("The selected profile file exceeds the 4 MiB import limit".to_string());
    }
    if bytes.len() as u64 != opened_metadata.len() {
        return Err("The selected profile changed while it was being staged".to_string());
    }
    let json_bytes = bytes.strip_prefix(&[0xEF, 0xBB, 0xBF]).unwrap_or(&bytes);
    let profile: serde_json::Value = serde_json::from_slice(json_bytes)
        .map_err(|_| "The selected file is not valid UTF-8 JSON".to_string())?;
    if !profile.is_object() {
        return Err("Canonical profile JSON must contain an object at the top level".to_string());
    }

    let staging_root = managed_directory_chain(data_dir, &["imports", "staging"], true)?
        .ok_or_else(|| "Unable to create secure source staging".to_string())?;
    let stage_dir = staging_directory(data_dir, &normalized_scan_id)?;
    if stage_dir.parent() != Some(staging_root.as_path()) {
        return Err("The source staging path is invalid".to_string());
    }
    match fs::symlink_metadata(&stage_dir) {
        Ok(_) => return Err("A source scan with this ID already exists".to_string()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(_) => return Err("Unable to inspect source staging".to_string()),
    }
    fs::create_dir(&stage_dir).map_err(|_| "Unable to create secure source staging".to_string())?;
    if let Err(error) = secure_directory_permissions(&stage_dir) {
        let _ = cleanup_staging(data_dir, &normalized_scan_id);
        return Err(error);
    }

    let staged_path = stage_dir.join("0000.json");
    let staged_result = (|| {
        let mut destination = open_staging_file(&staged_path)?;
        destination
            .write_all(&bytes)
            .map_err(|_| "The canonical profile snapshot could not be staged".to_string())?;
        destination
            .sync_all()
            .map_err(|_| "The canonical profile snapshot could not be finalized".to_string())?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&staged_path, fs::Permissions::from_mode(0o600))
                .map_err(|_| "The canonical profile snapshot could not be secured".to_string())?;
        }
        Ok::<_, String>(())
    })();
    if let Err(error) = staged_result {
        let _ = cleanup_staging(data_dir, &normalized_scan_id);
        return Err(error);
    }

    Ok(StagedCanonicalProfile {
        scan_id: normalized_scan_id.clone(),
        managed_relative_path: format!("imports/staging/{normalized_scan_id}/0000.json"),
        display_name: canonical_display_name(selected_file),
        raw_sha256: format!("{:x}", Sha256::digest(&bytes)),
        raw_bytes: bytes.len() as u64,
    })
}

fn stage_candidates(
    mut candidates: Vec<CandidateSource>,
    data_dir: &Path,
    scan_id: &str,
    discovered: usize,
    skipped: usize,
    mut issues: BTreeMap<String, usize>,
    selection_label: &str,
) -> Result<StagedSourceFolder, String> {
    let normalized_scan_id = safe_scan_id(scan_id)?;
    candidates.sort_by(compare_candidates);
    if candidates.len() > MAX_SOURCE_FILES {
        let message = format!("The {selection_label} contains more than 64 supported files");
        return Err(if selection_label == "selection" {
            format!("source_selection_count_limit: {message}")
        } else {
            message
        });
    }
    let aggregate_bytes = candidates
        .iter()
        .try_fold(0_u64, |total, candidate| {
            total.checked_add(candidate.byte_size)
        })
        .ok_or_else(|| "The source selection byte count is invalid".to_string())?;
    if aggregate_bytes > MAX_TOTAL_SOURCE_BYTES {
        let message =
            format!("The supported source files exceed the 40 MiB {selection_label} limit");
        return Err(if selection_label == "selection" {
            format!("source_selection_size_limit: {message}")
        } else {
            message
        });
    }

    let staging_root = managed_directory_chain(data_dir, &["imports", "staging"], true)?
        .ok_or_else(|| "Unable to create secure source staging".to_string())?;
    let stage_dir = staging_directory(data_dir, &normalized_scan_id)?;
    if stage_dir.parent() != Some(staging_root.as_path()) {
        return Err("The source staging path is invalid".to_string());
    }
    match fs::symlink_metadata(&stage_dir) {
        Ok(_) => return Err("A source scan with this ID already exists".to_string()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(_) => return Err("Unable to inspect source staging".to_string()),
    }
    fs::create_dir(&stage_dir).map_err(|_| "Unable to create secure source staging".to_string())?;
    if let Err(error) = secure_directory_permissions(&stage_dir) {
        let _ = cleanup_staging(data_dir, &normalized_scan_id);
        return Err(error);
    }

    let staged_result = (|| {
        let mut staged = Vec::with_capacity(candidates.len());
        let mut seen_hashes: HashMap<String, usize> = HashMap::new();
        for (ordinal, candidate) in candidates.iter().enumerate() {
            let target = stage_dir.join(format!("{ordinal:04}.{}", candidate.suffix));
            let (byte_size, checksum_sha256) = copy_candidate(candidate, &target)?;
            if seen_hashes
                .insert(checksum_sha256.clone(), ordinal)
                .is_some()
            {
                *issues.entry("duplicate_content".to_string()).or_default() += 1;
            }
            let managed_relative_path = target
                .strip_prefix(data_dir)
                .map_err(|_| "A source staging path escaped the local vault".to_string())?
                .to_string_lossy()
                .replace('\\', "/");
            staged.push(StagedSource {
                ordinal,
                managed_relative_path,
                display_name: candidate.display_name.clone(),
                format: candidate.format.to_string(),
                byte_size,
                checksum_sha256,
            });
        }
        Ok::<_, String>(staged)
    })();

    let sources = match staged_result {
        Ok(sources) => sources,
        Err(error) => {
            let _ = cleanup_staging(data_dir, &normalized_scan_id);
            return Err(error);
        }
    };
    Ok(StagedSourceFolder {
        scan_id: normalized_scan_id,
        file_counts: SourceScanCounts {
            discovered,
            staged: sources.len(),
            skipped,
        },
        scan_issues: issues,
        sources,
    })
}

pub(crate) fn stage_source_files(
    selected_files: &[PathBuf],
    data_dir: &Path,
    scan_id: &str,
) -> Result<StagedSourceFolder, String> {
    if selected_files.is_empty() {
        return Err("source_selection_empty: Choose at least one profile source file".to_string());
    }
    if selected_files.len() > MAX_SOURCE_FILES {
        return Err(
            "source_selection_count_limit: The source selection contains more than 64 supported files"
                .to_string(),
        );
    }
    let data_root = fs::canonicalize(data_dir)
        .map_err(|_| "Unable to verify the local data directory".to_string())?;
    let mut candidates = Vec::with_capacity(selected_files.len());
    for path in selected_files {
        let display_path = path
            .file_name()
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("source-document"));
        let candidate = match inspect_candidate(path, &display_path, &data_root)? {
            CandidateInspection::Candidate(candidate) => candidate,
            CandidateInspection::Rejected(reason) => {
                return Err(reason.selected_file_error().to_string());
            }
        };
        if candidates
            .iter()
            .any(|existing| same_selected_file(existing, &candidate))
        {
            return Err(
                "source_file_duplicate: The same source file was selected more than once"
                    .to_string(),
            );
        }
        candidates.push(candidate);
    }

    stage_candidates(
        candidates,
        data_dir,
        scan_id,
        selected_files.len(),
        0,
        BTreeMap::new(),
        "selection",
    )
}

pub(crate) fn stage_source_folder(
    selected_folder: &Path,
    data_dir: &Path,
    scan_id: &str,
) -> Result<StagedSourceFolder, String> {
    safe_scan_id(scan_id)?;
    let selected_metadata = fs::symlink_metadata(selected_folder)
        .map_err(|_| "The selected source folder could not be inspected".to_string())?;
    if selected_metadata.file_type().is_symlink() || !selected_metadata.is_dir() {
        return Err("Choose a regular folder of profile source documents".to_string());
    }
    let selected_root = fs::canonicalize(selected_folder)
        .map_err(|_| "The selected source folder could not be opened".to_string())?;
    let data_root = fs::canonicalize(data_dir)
        .map_err(|_| "Unable to verify the local data directory".to_string())?;

    let mut stack = vec![(selected_root.clone(), PathBuf::new(), 0_usize)];
    let mut candidates = Vec::new();
    let mut examined = 0_usize;
    let mut discovered = 0_usize;
    let mut skipped = 0_usize;
    let mut issues: BTreeMap<String, usize> = BTreeMap::new();

    while let Some((directory, relative_directory, depth)) = stack.pop() {
        let mut entries = fs::read_dir(&directory)
            .map_err(|_| "A source folder could not be read".to_string())?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|_| "A source folder entry could not be inspected".to_string())?;
        entries.sort_by_key(|entry| entry.file_name().to_string_lossy().to_lowercase());

        for entry in entries {
            examined += 1;
            if examined > MAX_ENTRIES_EXAMINED {
                return Err(
                    "The source folder contains too many entries (maximum 2,000)".to_string(),
                );
            }
            let name = entry.file_name();
            let name_text = name.to_string_lossy();
            let entry_path = entry.path();
            let relative_path = relative_directory.join(&name);
            let file_type = entry
                .file_type()
                .map_err(|_| "A source folder entry could not be inspected".to_string())?;

            if file_type.is_symlink() {
                skipped += 1;
                *issues.entry("symlink_skipped".to_string()).or_default() += 1;
                continue;
            }
            if file_type.is_dir() {
                let canonical_entry = fs::canonicalize(&entry_path)
                    .map_err(|_| "A source folder entry could not be verified".to_string())?;
                if !canonical_entry.starts_with(&selected_root) {
                    skipped += 1;
                    *issues.entry("path_escape_skipped".to_string()).or_default() += 1;
                    continue;
                }
                if canonical_entry.starts_with(&data_root) {
                    skipped += 1;
                    *issues
                        .entry("managed_folder_skipped".to_string())
                        .or_default() += 1;
                    continue;
                }
                if should_skip_directory(&name_text) {
                    skipped += 1;
                    *issues
                        .entry("hidden_folder_skipped".to_string())
                        .or_default() += 1;
                } else if depth >= MAX_SCAN_DEPTH {
                    skipped += 1;
                    *issues.entry("depth_limit_reached".to_string()).or_default() += 1;
                } else {
                    stack.push((canonical_entry, relative_path, depth + 1));
                }
                continue;
            }
            if !file_type.is_file() {
                skipped += 1;
                *issues
                    .entry("non_regular_file_skipped".to_string())
                    .or_default() += 1;
                continue;
            }
            discovered += 1;
            match inspect_candidate(&entry_path, &relative_path, &data_root)? {
                CandidateInspection::Candidate(candidate) => {
                    if !candidate.canonical_path.starts_with(&selected_root) {
                        skipped += 1;
                        *issues.entry("path_escape_skipped".to_string()).or_default() += 1;
                    } else {
                        candidates.push(candidate);
                    }
                }
                CandidateInspection::Rejected(reason) => {
                    skipped += 1;
                    *issues.entry(reason.issue_code().to_string()).or_default() += 1;
                }
            }
        }
    }

    stage_candidates(
        candidates, data_dir, scan_id, discovered, skipped, issues, "folder",
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn create_sparse_file(path: &Path, byte_size: u64) {
        let file = File::create(path).unwrap();
        file.set_len(byte_size).unwrap();
    }

    #[test]
    fn canonical_profile_staging_preserves_the_exact_validated_bytes() {
        let selected = tempfile::tempdir().unwrap();
        let data = tempfile::tempdir().unwrap();
        let private_folder = selected.path().join("private-profile-folder");
        fs::create_dir(&private_folder).unwrap();
        let source = private_folder.join("canonical profile.json.txt");
        let exact_bytes = b"\xEF\xBB\xBF \n{\n  \"basics\": {\"name\": \"Ada\"}\n}\n\t";
        fs::write(&source, exact_bytes).unwrap();
        let scan_id = Uuid::new_v4().to_string();

        let staged = stage_canonical_profile(&source, data.path(), &scan_id).unwrap();

        assert_eq!(staged.scan_id, scan_id);
        assert_eq!(
            staged.managed_relative_path,
            format!("imports/staging/{scan_id}/0000.json")
        );
        assert_eq!(staged.display_name, "canonical profile.json.txt");
        assert_eq!(staged.raw_bytes, exact_bytes.len() as u64);
        assert_eq!(
            staged.raw_sha256,
            format!("{:x}", Sha256::digest(exact_bytes))
        );
        assert_eq!(
            fs::read(data.path().join(&staged.managed_relative_path)).unwrap(),
            exact_bytes
        );
        assert!(
            !staged
                .managed_relative_path
                .contains("private-profile-folder")
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                data.path()
                    .join("imports/staging")
                    .join(&scan_id)
                    .metadata()
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o700
            );
            assert_eq!(
                data.path()
                    .join(&staged.managed_relative_path)
                    .metadata()
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
        }
    }

    #[test]
    fn canonical_profile_staging_rejects_invalid_or_managed_inputs_without_a_snapshot() {
        let selected = tempfile::tempdir().unwrap();
        let data = tempfile::tempdir().unwrap();
        let invalid = selected.path().join("canonical.json");
        fs::write(&invalid, b"[]").unwrap();
        let invalid_scan_id = Uuid::new_v4().to_string();
        assert!(stage_canonical_profile(&invalid, data.path(), &invalid_scan_id).is_err());
        assert!(
            !data
                .path()
                .join("imports/staging")
                .join(invalid_scan_id)
                .exists()
        );

        let managed = data.path().join("already-managed.json");
        fs::write(&managed, b"{}").unwrap();
        let managed_scan_id = Uuid::new_v4().to_string();
        assert!(stage_canonical_profile(&managed, data.path(), &managed_scan_id).is_err());
        assert!(
            !data
                .path()
                .join("imports/staging")
                .join(managed_scan_id)
                .exists()
        );

        let oversized = selected.path().join("oversized.json");
        create_sparse_file(&oversized, MAX_CANONICAL_PROFILE_BYTES + 1);
        let oversized_scan_id = Uuid::new_v4().to_string();
        assert!(stage_canonical_profile(&oversized, data.path(), &oversized_scan_id).is_err());
        assert!(
            !data
                .path()
                .join("imports/staging")
                .join(oversized_scan_id)
                .exists()
        );
    }

    #[cfg(unix)]
    #[test]
    fn canonical_profile_staging_never_follows_a_symlink() {
        use std::os::unix::fs::symlink;

        let selected = tempfile::tempdir().unwrap();
        let data = tempfile::tempdir().unwrap();
        let target = selected.path().join("target.json");
        let link = selected.path().join("canonical.json");
        fs::write(&target, b"{}").unwrap();
        symlink(&target, &link).unwrap();
        let scan_id = Uuid::new_v4().to_string();

        assert!(stage_canonical_profile(&link, data.path(), &scan_id).is_err());
        assert!(!data.path().join("imports/staging").join(scan_id).exists());
    }

    #[test]
    fn selected_files_are_staged_deterministically_and_opaquely() {
        let selected = tempfile::tempdir().unwrap();
        let data = tempfile::tempdir().unwrap();
        let first_dir = selected.path().join("private-one");
        let second_dir = selected.path().join("private-two");
        fs::create_dir(&first_dir).unwrap();
        fs::create_dir(&second_dir).unwrap();
        let zulu = first_dir.join("zulu.txt");
        let alpha = second_dir.join("Alpha.markdown");
        let middle = first_dir.join("middle.csv");
        fs::write(&zulu, b"Rust").unwrap();
        fs::write(&alpha, b"# Experience").unwrap();
        fs::write(&middle, b"skill,years\nSQL,5\n").unwrap();

        let scan_id = Uuid::new_v4().to_string();
        let staged = stage_source_files(
            &[zulu.clone(), middle.clone(), alpha.clone()],
            data.path(),
            &scan_id,
        )
        .unwrap();

        assert_eq!(staged.file_counts.discovered, 3);
        assert_eq!(staged.file_counts.staged, 3);
        assert_eq!(staged.file_counts.skipped, 0);
        assert_eq!(
            staged
                .sources
                .iter()
                .map(|source| source.display_name.as_str())
                .collect::<Vec<_>>(),
            vec!["Alpha.markdown", "middle.csv", "zulu.txt"]
        );
        assert_eq!(staged.sources[0].format, "md");
        for (ordinal, source) in staged.sources.iter().enumerate() {
            assert_eq!(source.ordinal, ordinal);
            assert_eq!(
                source.managed_relative_path,
                format!("imports/staging/{scan_id}/{ordinal:04}.{}", source.format)
            );
            assert!(!source.display_name.contains("private-one"));
            assert!(!source.display_name.contains("private-two"));
            assert!(!source.managed_relative_path.contains("private-one"));
            assert!(!source.managed_relative_path.contains("private-two"));
        }
    }

    #[test]
    fn selected_files_report_duplicate_content_and_reject_repeat_selection() {
        let selected = tempfile::tempdir().unwrap();
        let data = tempfile::tempdir().unwrap();
        let first = selected.path().join("first.txt");
        let second = selected.path().join("second.md");
        fs::write(&first, b"same evidence").unwrap();
        fs::write(&second, b"same evidence").unwrap();

        let scan_id = Uuid::new_v4().to_string();
        let staged = stage_source_files(&[second, first.clone()], data.path(), &scan_id).unwrap();
        assert_eq!(staged.sources.len(), 2);
        assert_eq!(staged.scan_issues.get("duplicate_content"), Some(&1));

        let duplicate_scan_id = Uuid::new_v4().to_string();
        let error = stage_source_files(&[first.clone(), first], data.path(), &duplicate_scan_id)
            .unwrap_err();
        assert!(error.starts_with("source_file_duplicate:"));
        assert!(
            !data
                .path()
                .join("imports/staging")
                .join(duplicate_scan_id)
                .exists()
        );
    }

    #[test]
    fn selected_files_enforce_count_per_file_and_total_bounds_before_staging() {
        let selected = tempfile::tempdir().unwrap();
        let data = tempfile::tempdir().unwrap();
        let mut too_many = Vec::new();
        for index in 0..=MAX_SOURCE_FILES {
            let path = selected.path().join(format!("source-{index}.txt"));
            fs::write(&path, b"evidence").unwrap();
            too_many.push(path);
        }
        let count_scan_id = Uuid::new_v4().to_string();
        assert!(
            stage_source_files(&too_many, data.path(), &count_scan_id)
                .unwrap_err()
                .starts_with("source_selection_count_limit:")
        );
        assert!(
            !data
                .path()
                .join("imports/staging")
                .join(count_scan_id)
                .exists()
        );

        let oversized_text = selected.path().join("oversized.txt");
        create_sparse_file(&oversized_text, MAX_TEXT_SOURCE_BYTES + 1);
        let per_file_scan_id = Uuid::new_v4().to_string();
        assert!(
            stage_source_files(&[oversized_text], data.path(), &per_file_scan_id)
                .unwrap_err()
                .starts_with("source_file_size_limit:")
        );
        assert!(
            !data
                .path()
                .join("imports/staging")
                .join(per_file_scan_id)
                .exists()
        );

        let mut over_total = Vec::new();
        for index in 0..5 {
            let path = selected.path().join(format!("structured-{index}.pdf"));
            create_sparse_file(&path, MAX_STRUCTURED_SOURCE_BYTES);
            over_total.push(path);
        }
        let total_scan_id = Uuid::new_v4().to_string();
        assert!(
            stage_source_files(&over_total, data.path(), &total_scan_id)
                .unwrap_err()
                .starts_with("source_selection_size_limit:")
        );
        assert!(
            !data
                .path()
                .join("imports/staging")
                .join(total_scan_id)
                .exists()
        );
    }

    #[test]
    fn selected_files_reject_unsupported_and_non_regular_inputs() {
        let selected = tempfile::tempdir().unwrap();
        let data = tempfile::tempdir().unwrap();
        let unsupported = selected.path().join("portrait.png");
        fs::write(&unsupported, b"not a source").unwrap();
        assert!(
            stage_source_files(&[unsupported], data.path(), &Uuid::new_v4().to_string())
                .unwrap_err()
                .starts_with("source_file_type_unsupported:")
        );
        assert!(
            stage_source_files(
                &[selected.path().to_path_buf()],
                data.path(),
                &Uuid::new_v4().to_string()
            )
            .unwrap_err()
            .starts_with("source_file_non_regular:")
        );
    }

    #[test]
    fn selected_files_emit_stable_empty_selection_and_file_error_codes() {
        let selected = tempfile::tempdir().unwrap();
        let data = tempfile::tempdir().unwrap();
        let empty = selected.path().join("empty.txt");
        let managed = data.path().join("managed.txt");
        fs::write(&empty, b"").unwrap();
        fs::write(&managed, b"managed evidence").unwrap();

        assert!(
            stage_source_files(&[], data.path(), &Uuid::new_v4().to_string())
                .unwrap_err()
                .starts_with("source_selection_empty:")
        );
        assert!(
            stage_source_files(&[empty], data.path(), &Uuid::new_v4().to_string())
                .unwrap_err()
                .starts_with("source_file_empty:")
        );
        assert!(
            stage_source_files(&[managed], data.path(), &Uuid::new_v4().to_string())
                .unwrap_err()
                .starts_with("source_file_managed:")
        );
    }

    #[cfg(unix)]
    #[test]
    fn selected_files_reject_symlinks_without_creating_staging() {
        use std::os::unix::fs::symlink;

        let selected = tempfile::tempdir().unwrap();
        let data = tempfile::tempdir().unwrap();
        let target = selected.path().join("target.txt");
        let link = selected.path().join("link.txt");
        fs::write(&target, b"private evidence").unwrap();
        symlink(&target, &link).unwrap();
        let scan_id = Uuid::new_v4().to_string();

        assert!(
            stage_source_files(&[link], data.path(), &scan_id)
                .unwrap_err()
                .starts_with("source_file_symlink:")
        );
        assert!(!data.path().join("imports/staging").join(scan_id).exists());
    }

    #[cfg(windows)]
    #[test]
    fn windows_source_handle_uses_stable_identity_and_excludes_mutation() {
        let selected = tempfile::tempdir().unwrap();
        let source_path = selected.path().join("resume.txt");
        let hard_link_path = selected.path().join("resume-hard-link.txt");
        fs::write(&source_path, b"private evidence").unwrap();
        fs::hard_link(&source_path, &hard_link_path).unwrap();

        let source = open_source_file(&source_path).unwrap();
        let hard_link = open_source_file(&hard_link_path).unwrap();
        assert_eq!(
            windows_file_identity(&source).unwrap(),
            windows_file_identity(&hard_link).unwrap()
        );

        assert!(OpenOptions::new().write(true).open(&source_path).is_err());
        assert!(
            fs::rename(&source_path, selected.path().join("moved.txt")).is_err(),
            "the held source handle must deny delete/rename sharing"
        );
    }

    #[cfg(windows)]
    #[test]
    fn windows_source_handle_rejects_a_reparse_point() {
        use std::os::windows::fs::symlink_file;

        let selected = tempfile::tempdir().unwrap();
        let source_path = selected.path().join("resume.txt");
        let link_path = selected.path().join("resume-link.txt");
        fs::write(&source_path, b"private evidence").unwrap();
        if symlink_file(&source_path, &link_path).is_err() {
            return;
        }

        if let Ok(link) = open_source_file(&link_path) {
            assert!(windows_file_identity(&link).is_err());
        }
    }

    #[test]
    fn stages_supported_files_deterministically_and_opaquely() {
        let selected = tempfile::tempdir().unwrap();
        let data = tempfile::tempdir().unwrap();
        fs::create_dir(selected.path().join("nested")).unwrap();
        fs::write(selected.path().join("z.txt"), b"skills: Rust, SQL").unwrap();
        fs::write(
            selected.path().join("nested/a.md"),
            b"# Ada\n\n## Experience",
        )
        .unwrap();
        fs::write(selected.path().join("ignored.png"), b"png").unwrap();

        let scan_id = Uuid::new_v4().to_string();
        let staged = stage_source_folder(selected.path(), data.path(), &scan_id).unwrap();

        assert_eq!(staged.file_counts.discovered, 3);
        assert_eq!(staged.file_counts.staged, 2);
        assert_eq!(staged.sources[0].display_name, "nested/a.md");
        assert_eq!(staged.sources[1].display_name, "z.txt");
        for source in &staged.sources {
            assert!(
                source
                    .managed_relative_path
                    .starts_with(&format!("imports/staging/{scan_id}/"))
            );
            assert!(
                !source
                    .managed_relative_path
                    .contains(selected.path().to_string_lossy().as_ref())
            );
            assert_eq!(
                data.path()
                    .join(&source.managed_relative_path)
                    .metadata()
                    .unwrap()
                    .len(),
                source.byte_size
            );
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let stage_dir = data.path().join("imports/staging").join(&scan_id);
            assert_eq!(
                stage_dir.metadata().unwrap().permissions().mode() & 0o777,
                0o700
            );
            assert_eq!(
                data.path()
                    .join(&staged.sources[0].managed_relative_path)
                    .metadata()
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
        }
        cleanup_staging(data.path(), &scan_id).unwrap();
        assert!(!data.path().join("imports/staging").join(scan_id).exists());
    }

    #[cfg(unix)]
    #[test]
    fn source_scan_never_follows_symlinks() {
        use std::os::unix::fs::symlink;

        let selected = tempfile::tempdir().unwrap();
        let data = tempfile::tempdir().unwrap();
        fs::write(selected.path().join("real.txt"), b"real").unwrap();
        symlink(
            selected.path().join("real.txt"),
            selected.path().join("linked.txt"),
        )
        .unwrap();

        let scan_id = Uuid::new_v4().to_string();
        let staged = stage_source_folder(selected.path(), data.path(), &scan_id).unwrap();
        assert_eq!(staged.sources.len(), 1);
        assert_eq!(staged.scan_issues.get("symlink_skipped"), Some(&1));
    }

    #[cfg(unix)]
    #[test]
    fn managed_staging_never_follows_a_symlinked_ancestor() {
        use std::os::unix::fs::symlink;

        let selected = tempfile::tempdir().unwrap();
        let data = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        fs::write(selected.path().join("resume.txt"), b"Ada Lovelace").unwrap();
        fs::create_dir(data.path().join("imports")).unwrap();
        symlink(outside.path(), data.path().join("imports").join("staging")).unwrap();
        let scan_id = Uuid::new_v4().to_string();
        let outside_scan = outside.path().join(&scan_id);
        fs::create_dir(&outside_scan).unwrap();
        let marker = outside_scan.join("must-remain.txt");
        fs::write(&marker, b"outside the vault").unwrap();

        assert!(stage_source_folder(selected.path(), data.path(), &scan_id).is_err());
        assert!(cleanup_staging(data.path(), &scan_id).is_err());
        assert_eq!(fs::read(&marker).unwrap(), b"outside the vault");
    }

    #[cfg(unix)]
    #[test]
    fn staging_rejects_same_size_file_replacement_after_discovery() {
        let selected = tempfile::tempdir().unwrap();
        let data = tempfile::tempdir().unwrap();
        let source = selected.path().join("resume.txt");
        fs::write(&source, b"first").unwrap();
        let candidate =
            match inspect_candidate(&source, Path::new("resume.txt"), data.path()).unwrap() {
                CandidateInspection::Candidate(candidate) => candidate,
                CandidateInspection::Rejected(reason) => {
                    panic!("unexpected candidate rejection: {reason:?}")
                }
            };
        let replacement = selected.path().join("replacement.txt");
        fs::write(&replacement, b"other").unwrap();
        fs::rename(&replacement, &source).unwrap();

        let scan_id = Uuid::new_v4().to_string();
        assert!(
            stage_candidates(
                vec![candidate],
                data.path(),
                &scan_id,
                1,
                0,
                BTreeMap::new(),
                "selection",
            )
            .is_err()
        );
        assert!(!data.path().join("imports/staging").join(scan_id).exists());
    }

    #[test]
    fn source_scan_rejects_global_file_count_overflow_without_staging() {
        let selected = tempfile::tempdir().unwrap();
        let data = tempfile::tempdir().unwrap();
        for index in 0..=MAX_SOURCE_FILES {
            fs::write(
                selected.path().join(format!("source-{index}.txt")),
                b"evidence",
            )
            .unwrap();
        }
        let scan_id = Uuid::new_v4().to_string();
        assert!(stage_source_folder(selected.path(), data.path(), &scan_id).is_err());
        assert!(!data.path().join("imports/staging").join(scan_id).exists());
    }
}
