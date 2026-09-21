// SPDX-License-Identifier: MPL-2.0
import type { FormEvent, RefObject } from "react";
import "./ProfileCollections.css";

export type EditableProfileSection = "projects" | "education" | "skills";

export type ProfileProjectEntry = {
  entry_index: number;
  name: string;
  description: string | null;
  url: string | null;
  start_date: string | null;
  end_date: string | null;
  highlights: string[];
  keywords: string[];
};

export type ProfileEducationEntry = {
  entry_index: number;
  institution: string;
  url: string | null;
  area: string | null;
  study_type: string | null;
  start_date: string | null;
  end_date: string | null;
  score: string | null;
  courses: string[];
};

export type ProfileSkillEntry = {
  entry_index: number;
  name: string;
  level: string | null;
  keywords: string[];
};

export type ProfileCollectionEntry =
  | ProfileProjectEntry
  | ProfileEducationEntry
  | ProfileSkillEntry;

export type ProfileCollectionPage = {
  profile_version_id: string;
  version_number: number;
  section: EditableProfileSection;
  total_items: number;
  offset: number;
  limit: number;
  next_offset: number | null;
  items: ProfileCollectionEntry[];
};

export type ProfileCollectionOperation =
  | { kind: "add"; entry: Record<string, string | string[] | null> }
  | { kind: "update"; entry_index: number; patch: Record<string, string | string[] | null> }
  | { kind: "remove"; entry_index: number };

export type ProfileCollectionUpdateReceipt = {
  request_id: string;
  profile_version_id: string;
  parent_profile_version_id: string;
  version_number: number;
  created_at_ms: number;
  created: boolean;
  section: EditableProfileSection;
  operation: "add" | "update" | "remove";
  entry_index: number;
  changed_fields: string[];
  profile_name: string;
  section_entries: number;
  renderable: boolean;
};

export type ProjectForm = {
  section: "projects";
  name: string;
  description: string;
  url: string;
  startDate: string;
  endDate: string;
  highlights: string;
  keywords: string;
};

export type EducationForm = {
  section: "education";
  institution: string;
  url: string;
  area: string;
  studyType: string;
  startDate: string;
  endDate: string;
  score: string;
  courses: string;
};

export type SkillForm = {
  section: "skills";
  name: string;
  level: string;
  keywords: string;
};

export type ProfileCollectionForm = ProjectForm | EducationForm | SkillForm;

export const EDITABLE_PROFILE_SECTIONS: readonly EditableProfileSection[] = [
  "projects",
  "education",
  "skills",
];

const SECTION_COPY: Record<EditableProfileSection, {
  label: string;
  singular: string;
  plural: string;
  addLabel: string;
}> = {
  projects: {
    label: "Projects & experiences",
    singular: "project",
    plural: "projects",
    addLabel: "Add project",
  },
  education: {
    label: "Education",
    singular: "education entry",
    plural: "education entries",
    addLabel: "Add education",
  },
  skills: {
    label: "Skills",
    singular: "skill group",
    plural: "skill groups",
    addLabel: "Add skill group",
  },
};

export function isEditableProfileSection(value: string): value is EditableProfileSection {
  return EDITABLE_PROFILE_SECTIONS.some((section) => section === value);
}

export function collectionLabel(section: EditableProfileSection): string {
  return SECTION_COPY[section].label;
}

export function collectionSingular(section: EditableProfileSection): string {
  return SECTION_COPY[section].singular;
}

function nullable(value: string): string | null {
  const cleaned = value.trim();
  return cleaned || null;
}

export function normalizedLines(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

function lines(value: string[]): string {
  return value.join("\n");
}

export function emptyCollectionForm(section: EditableProfileSection): ProfileCollectionForm {
  if (section === "projects") {
    return {
      section,
      name: "",
      description: "",
      url: "",
      startDate: "",
      endDate: "",
      highlights: "",
      keywords: "",
    };
  }
  if (section === "education") {
    return {
      section,
      institution: "",
      url: "",
      area: "",
      studyType: "",
      startDate: "",
      endDate: "",
      score: "",
      courses: "",
    };
  }
  return { section, name: "", level: "", keywords: "" };
}

export function collectionFormFromEntry(
  section: EditableProfileSection,
  entry: ProfileCollectionEntry,
): ProfileCollectionForm {
  if (section === "projects") {
    const project = entry as ProfileProjectEntry;
    return {
      section,
      name: project.name,
      description: project.description ?? "",
      url: project.url ?? "",
      startDate: project.start_date ?? "",
      endDate: project.end_date ?? "",
      highlights: lines(project.highlights),
      keywords: lines(project.keywords),
    };
  }
  if (section === "education") {
    const education = entry as ProfileEducationEntry;
    return {
      section,
      institution: education.institution,
      url: education.url ?? "",
      area: education.area ?? "",
      studyType: education.study_type ?? "",
      startDate: education.start_date ?? "",
      endDate: education.end_date ?? "",
      score: education.score ?? "",
      courses: lines(education.courses),
    };
  }
  const skill = entry as ProfileSkillEntry;
  return {
    section,
    name: skill.name,
    level: skill.level ?? "",
    keywords: lines(skill.keywords),
  };
}

export function collectionInputFromForm(
  form: ProfileCollectionForm,
): Record<string, string | string[] | null> {
  if (form.section === "projects") {
    return {
      name: form.name.trim(),
      description: nullable(form.description),
      url: nullable(form.url),
      start_date: nullable(form.startDate),
      end_date: nullable(form.endDate),
      highlights: normalizedLines(form.highlights),
      keywords: normalizedLines(form.keywords),
    };
  }
  if (form.section === "education") {
    return {
      institution: form.institution.trim(),
      url: nullable(form.url),
      area: nullable(form.area),
      study_type: nullable(form.studyType),
      start_date: nullable(form.startDate),
      end_date: nullable(form.endDate),
      score: nullable(form.score),
      courses: normalizedLines(form.courses),
    };
  }
  return {
    name: form.name.trim(),
    level: nullable(form.level),
    keywords: normalizedLines(form.keywords),
  };
}

function comparableEntry(
  section: EditableProfileSection,
  entry: ProfileCollectionEntry,
): Record<string, string | string[] | null> {
  if (section === "projects") {
    const project = entry as ProfileProjectEntry;
    return {
      name: project.name,
      description: project.description,
      url: project.url,
      start_date: project.start_date,
      end_date: project.end_date,
      highlights: project.highlights,
      keywords: project.keywords,
    };
  }
  if (section === "education") {
    const education = entry as ProfileEducationEntry;
    return {
      institution: education.institution,
      url: education.url,
      area: education.area,
      study_type: education.study_type,
      start_date: education.start_date,
      end_date: education.end_date,
      score: education.score,
      courses: education.courses,
    };
  }
  const skill = entry as ProfileSkillEntry;
  return { name: skill.name, level: skill.level, keywords: skill.keywords };
}

export function buildCollectionPatch(
  section: EditableProfileSection,
  original: ProfileCollectionEntry,
  form: ProfileCollectionForm,
): Record<string, string | string[] | null> {
  const before = comparableEntry(section, original);
  const after = collectionInputFromForm(form);
  return Object.fromEntries(
    Object.entries(after)
      .filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(before[key]))
      .map(([key, value]) => [
        key,
        Array.isArray(value) && value.length === 0 && section !== "skills" ? null : value,
      ]),
  );
}

export function collectionFormHasInput(form: ProfileCollectionForm): boolean {
  return Object.entries(form).some(([key, value]) => key !== "section" && value.trim().length > 0);
}

export type CollectionValidation = { message: string; field: string } | null;

function parseIpv4(host: string): [number, number, number, number] | null {
  const parts = host.split(".");
  if (parts.length !== 4 || parts.some((part) => !/^\d{1,3}$/.test(part))) return null;
  const octets = parts.map(Number);
  if (octets.some((octet) => octet < 0 || octet > 255)) return null;
  return octets as [number, number, number, number];
}

function ipv4IsPublic([first, second, third, fourth]: [number, number, number, number]): boolean {
  return !(
    first === 0
    || first === 10
    || first === 127
    || (first === 100 && second >= 64 && second <= 127)
    || (first === 169 && second === 254)
    || (first === 172 && second >= 16 && second <= 31)
    || (first === 192 && second === 0 && third === 0)
    || (first === 192 && second === 0 && third === 2)
    || (first === 192 && second === 168)
    || (first === 198 && (second === 18 || second === 19))
    || (first === 198 && second === 51 && third === 100)
    || (first === 203 && second === 0 && third === 113)
    || first >= 224
    || (first === 255 && second === 255 && third === 255 && fourth === 255)
  );
}

function parseIpv6(host: string): number[] | null {
  const halves = host.split("::");
  if (halves.length > 2) return null;
  const parseHalf = (half: string): number[] | null => {
    if (!half) return [];
    const parts = half.split(":");
    const words: number[] = [];
    for (const [index, part] of parts.entries()) {
      if (part.includes(".")) {
        if (index !== parts.length - 1) return null;
        const ipv4 = parseIpv4(part);
        if (!ipv4) return null;
        words.push((ipv4[0] << 8) | ipv4[1], (ipv4[2] << 8) | ipv4[3]);
      } else {
        if (!/^[0-9a-f]{1,4}$/i.test(part)) return null;
        words.push(Number.parseInt(part, 16));
      }
    }
    return words;
  };
  const left = parseHalf(halves[0]);
  const right = parseHalf(halves[1] ?? "");
  if (!left || !right) return null;
  if (halves.length === 1) return left.length === 8 ? left : null;
  const missing = 8 - left.length - right.length;
  if (missing < 1) return null;
  return [...left, ...Array<number>(missing).fill(0), ...right];
}

function ipv6IsPublic(host: string): boolean {
  const words = parseIpv6(host);
  if (!words) return false;
  const allZero = words.every((word) => word === 0);
  const loopback = words.slice(0, 7).every((word) => word === 0) && words[7] === 1;
  const uniqueLocal = (words[0] & 0xfe00) === 0xfc00;
  const linkLocal = (words[0] & 0xffc0) === 0xfe80;
  const multicast = (words[0] & 0xff00) === 0xff00;
  const documentation = words[0] === 0x2001 && words[1] === 0x0db8;
  const mappedIpv4 = words.slice(0, 5).every((word) => word === 0)
    && (words[5] === 0 || words[5] === 0xffff)
    ? [words[6] >> 8, words[6] & 0xff, words[7] >> 8, words[7] & 0xff] as [number, number, number, number]
    : null;
  return !(
    allZero
    || loopback
    || uniqueLocal
    || linkLocal
    || multicast
    || documentation
    || (mappedIpv4 !== null && !ipv4IsPublic(mappedIpv4))
  );
}

function urlHostIsPublic(hostname: string): boolean {
  const host = hostname.toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
  if (!host) return false;
  const ipv4 = parseIpv4(host);
  if (ipv4) return ipv4IsPublic(ipv4);
  if (host.includes(":")) return ipv6IsPublic(host);
  const reservedSuffixes = [
    "localhost",
    "localhost.localdomain",
    "local",
    "internal",
    "home",
    "lan",
    "invalid",
    "test",
    "example",
  ];
  if (reservedSuffixes.some((suffix) => host === suffix || host.endsWith(`.${suffix}`))) {
    return false;
  }
  const labels = host.split(".");
  return labels.length >= 2
    && host.length <= 253
    && labels.every((label) => (
      label.length > 0
      && label.length <= 63
      && /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/i.test(label)
    ));
}

function validatePublicUrl(value: string, label: string): CollectionValidation {
  if (!/^https?:\/\//i.test(value) || /\s/.test(value) || value.includes("\\")) {
    return { message: `${label} must be an absolute HTTP or HTTPS URL without spaces or backslashes.`, field: "url" };
  }
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return { message: `${label} must be an absolute HTTP or HTTPS URL.`, field: "url" };
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return { message: `${label} must use http:// or https://.`, field: "url" };
  }
  if (parsed.username || parsed.password) {
    return { message: `${label} cannot include a username or password.`, field: "url" };
  }
  if (!urlHostIsPublic(parsed.hostname)) {
    return { message: `${label} must use a public internet host, not a local or private address.`, field: "url" };
  }
  return null;
}

export function validateCollectionForm(form: ProfileCollectionForm): CollectionValidation {
  const input = collectionInputFromForm(form);
  if (form.section === "projects" && !input.name) {
    return { message: "Project name is required.", field: "name" };
  }
  if (form.section === "education" && !input.institution) {
    return { message: "Institution is required.", field: "institution" };
  }
  if (form.section === "education" && !input.study_type && !input.area) {
    return {
      message: "Add a credential or an area of study.",
      field: "studyType",
    };
  }
  if (form.section === "skills" && !input.name) {
    return { message: "Skill group name is required.", field: "name" };
  }
  if (form.section === "skills" && (input.keywords as string[]).length === 0) {
    return { message: "Add at least one skill, one per line.", field: "keywords" };
  }
  if (form.section !== "skills" && typeof input.url === "string") {
    const urlError = validatePublicUrl(
      input.url,
      form.section === "projects" ? "Project URL" : "Institution URL",
    );
    if (urlError) return urlError;
  }
  const listLimits: Array<[string, number, number, number]> = form.section === "projects"
    ? [["highlights", 8, 360, 1_800], ["keywords", 16, 100, 1_200]]
    : form.section === "education"
      ? [["courses", 12, 160, 1_200]]
      : [["keywords", 18, 80, 1_200]];
  for (const [field, countLimit, itemLimit, totalLimit] of listLimits) {
    const values = input[field] as string[];
    if (values.length > countLimit) {
      return { message: `Use at most ${countLimit} ${field}.`, field };
    }
    if (values.some((value) => value.length > itemLimit)) {
      return { message: `Each ${field} item must be ${itemLimit} characters or fewer.`, field };
    }
    if (values.reduce((total, value) => total + value.length, 0) > totalLimit) {
      return { message: `${SECTION_COPY[form.section].label} ${field} must use ${totalLimit.toLocaleString()} characters or fewer in total.`, field };
    }
  }
  return null;
}

export function collectionEntryTitle(section: EditableProfileSection, entry: ProfileCollectionEntry): string {
  if (section === "education") {
    return (entry as ProfileEducationEntry).institution || "Untitled institution";
  }
  return (entry as ProfileProjectEntry | ProfileSkillEntry).name
    || (section === "skills" ? "Untitled skill group" : "Untitled project");
}

function dateRange(start: string | null, end: string | null): string {
  return [start, end].filter(Boolean).join(" – ");
}

function EntryBody({
  section,
  entry,
}: {
  section: EditableProfileSection;
  entry: ProfileCollectionEntry;
}) {
  if (section === "projects") {
    const project = entry as ProfileProjectEntry;
    const dates = dateRange(project.start_date, project.end_date);
    return (
      <>
        <header><div><h4>{project.name || "Untitled project"}</h4>{dates && <small>{dates}</small>}</div></header>
        {project.description && <p className="profile-review-prose">{project.description}</p>}
        {project.highlights.length > 0 && <ul className="profile-collection-entry__list">{project.highlights.map((item, index) => <li key={`${entry.entry_index}:highlight:${index}`}>{item}</li>)}</ul>}
        {project.keywords.length > 0 && <ul className="profile-review-tags" aria-label="Keywords">{project.keywords.map((item, index) => <li key={`${entry.entry_index}:keyword:${index}`}>{item}</li>)}</ul>}
        {project.url && <p className="profile-collection-entry__url">{project.url}</p>}
      </>
    );
  }
  if (section === "education") {
    const education = entry as ProfileEducationEntry;
    const credential = [education.study_type, education.area].filter(Boolean).join(" · ");
    const dates = dateRange(education.start_date, education.end_date);
    return (
      <>
        <header><div><h4>{education.institution || "Untitled institution"}</h4>{credential && <p>{credential}</p>}{dates && <small>{dates}</small>}</div></header>
        {education.score && <p className="profile-collection-entry__detail"><strong>Score</strong>{education.score}</p>}
        {education.courses.length > 0 && <ul className="profile-review-tags" aria-label="Courses">{education.courses.map((item, index) => <li key={`${entry.entry_index}:course:${index}`}>{item}</li>)}</ul>}
        {education.url && <p className="profile-collection-entry__url">{education.url}</p>}
      </>
    );
  }
  const skill = entry as ProfileSkillEntry;
  return (
    <>
      <header><div><h4>{skill.name || "Untitled skill group"}</h4>{skill.level && <p>{skill.level}</p>}</div></header>
      {skill.keywords.length > 0 && <ul className="profile-review-tags" aria-label="Skills">{skill.keywords.map((item, index) => <li key={`${entry.entry_index}:skill:${index}`}>{item}</li>)}</ul>}
    </>
  );
}

export function ProfileCollectionSection({
  section,
  profileVersionId,
  versionNumber,
  current,
  page,
  loading,
  loadingMore,
  error,
  busy,
  addDisabled,
  headingRef,
  onAdd,
  onEdit,
  onRemove,
  queuedStateForEntry,
  presentedEntry,
  onUndoQueued,
  onRetry,
  onLoadMore,
}: {
  section: EditableProfileSection;
  profileVersionId: string;
  versionNumber: number;
  current: boolean;
  page: ProfileCollectionPage | null;
  loading: boolean;
  loadingMore: boolean;
  error: string | null;
  busy: boolean;
  addDisabled: boolean;
  headingRef: RefObject<HTMLHeadingElement | null>;
  onAdd: () => void;
  onEdit: (entry: ProfileCollectionEntry) => void;
  onRemove: (entry: ProfileCollectionEntry, trigger: HTMLButtonElement) => void;
  queuedStateForEntry: (entry: ProfileCollectionEntry) => "edit" | "removal" | "merge" | "merged-away" | null;
  presentedEntry: (entry: ProfileCollectionEntry) => ProfileCollectionEntry;
  onUndoQueued: (entry: ProfileCollectionEntry) => void;
  onRetry: () => void;
  onLoadMore: () => void;
}) {
  const copy = SECTION_COPY[section];
  const items = page?.items ?? [];
  const canLoadMore = Boolean(page && page.next_offset !== null && items.length < page.total_items);
  return (
    <article className={`profile-review-document profile-collection profile-collection--${section}`} aria-labelledby={`profile-collection-${section}-title`}>
      <header className="profile-review-section-header">
        <div>
          <p className="section-kicker">Structured profile section</p>
          <h3 id={`profile-collection-${section}-title`} ref={headingRef} tabIndex={-1}>{copy.label}</h3>
          <p>{current ? "Queue review edits and removals, then apply them together as one immutable profile version." : `Read-only ${copy.label} from profile version ${versionNumber}.`}</p>
        </div>
        <div className="profile-collection__heading-actions">
          <span>{page?.total_items ?? 0} {(page?.total_items ?? 0) === 1 ? copy.singular : copy.plural}</span>
          {current && <button type="button" className="primary-action" onClick={onAdd} disabled={busy || addDisabled}>{copy.addLabel}</button>}
        </div>
      </header>

      {loading && !page && <div className="profile-review-section-loading" role="status"><span className="working-indicator" aria-hidden="true" />Opening {copy.label}…</div>}
      {!loading && error && !page && <div className="profile-review-inline-error" role="alert"><p>{error}</p><button type="button" className="secondary-action" onClick={onRetry}>Try again</button></div>}
      {!loading && !error && page && items.length === 0 && (
        <div className="profile-review-empty-section profile-review-empty-section--large">
          <div className="empty-document__glyph" aria-hidden="true">⌁</div>
          <h4>No {copy.label.toLowerCase()} yet</h4>
          <p>{current ? `Add the first ${copy.singular} to this profile.` : `This saved version has no ${copy.plural}.`}</p>
          {current && <button type="button" className="primary-action" onClick={onAdd} disabled={busy || addDisabled}>{copy.addLabel}</button>}
        </div>
      )}
      {page && items.length > 0 && (
        <div className="profile-collection__list">
          {items.map((entry, displayIndex) => {
            const visibleEntry = presentedEntry(entry);
            const title = collectionEntryTitle(section, visibleEntry);
            const queuedState = queuedStateForEntry(entry);
            return (
              <article
                key={`${profileVersionId}:${section}:${entry.entry_index}`}
                className={`profile-collection-entry${queuedState ? ` is-queued is-queued--${queuedState}` : ""}`}
              >
                <div className="profile-review-item__ordinal" aria-hidden="true">{String(displayIndex + 1).padStart(2, "0")}</div>
                <div className="profile-collection-entry__body">
                  <EntryBody section={section} entry={visibleEntry} />
                  {queuedState && (
                    <span className="profile-review-queued-label">
                      {queuedState === "edit"
                        ? "Edit queued"
                        : queuedState === "merge"
                          ? "Merge queued"
                          : queuedState === "merged-away" ? "Will merge into another entry" : "Removal queued"}
                    </span>
                  )}
                  {current && <div className="profile-collection-entry__actions">
                    {queuedState ? (
                      <>
                        {queuedState === "edit" && <button type="button" className="quiet-action" aria-label={`Revise queued edit to ${title}`} onClick={() => onEdit(entry)} disabled={busy}>Edit queued</button>}
                        <button type="button" className="quiet-action" data-profile-change-target={`${section}:${entry.entry_index}`} aria-label={`Undo queued change to ${title}`} onClick={() => onUndoQueued(entry)} disabled={busy}>Undo</button>
                      </>
                    ) : (
                      <>
                        <button type="button" className="quiet-action" aria-label={`Edit ${title}`} onClick={() => onEdit(entry)} disabled={busy}>Edit</button>
                        <button type="button" className="quiet-action profile-collection-entry__remove" aria-label={`Queue removal of ${title}`} onClick={(event) => onRemove(entry, event.currentTarget)} disabled={busy}>Remove</button>
                      </>
                    )}
                  </div>}
                </div>
              </article>
            );
          })}
        </div>
      )}
      {page && error && <div className="profile-review-inline-error profile-review-inline-error--pagination" role="alert"><p>{error}</p><button type="button" className="secondary-action" onClick={onLoadMore}>Try loading more again</button></div>}
      {page && <footer className="profile-review-pagination"><span>Showing {items.length} of {page.total_items}</span>{canLoadMore && !error && <button type="button" className="secondary-action" onClick={onLoadMore} disabled={loadingMore}>{loadingMore ? "Loading more…" : `Load more (${page.total_items - items.length})`}</button>}</footer>}
    </article>
  );
}

type FormProps = {
  form: ProfileCollectionForm;
  setForm: (next: ProfileCollectionForm) => void;
  firstInputRef: RefObject<HTMLInputElement | null>;
  errorId?: string;
};

function ProjectFields({ form, setForm, firstInputRef, errorId }: FormProps) {
  if (form.section !== "projects") return null;
  const update = (patch: Partial<ProjectForm>) => setForm({ ...form, ...patch });
  return <div className="profile-collection-editor__grid">
    <label className="profile-collection-editor__wide"><span>Project name <b aria-hidden="true">*</b></span><input ref={firstInputRef} data-profile-collection-field="name" value={form.name} onChange={(event) => update({ name: event.target.value })} maxLength={200} required aria-describedby={errorId} /></label>
    <label><span>Start date</span><input value={form.startDate} onChange={(event) => update({ startDate: event.target.value })} maxLength={80} placeholder="2022-04 or Spring 2022" /></label>
    <label><span>End date</span><input value={form.endDate} onChange={(event) => update({ endDate: event.target.value })} maxLength={80} placeholder="Present or 2025-08" /></label>
    <label className="profile-collection-editor__wide"><span>Project URL</span><input data-profile-collection-field="url" type="url" value={form.url} onChange={(event) => update({ url: event.target.value })} maxLength={2048} placeholder="https://example.com" aria-describedby={errorId} /></label>
    <label className="profile-collection-editor__wide"><span>Description</span><textarea value={form.description} onChange={(event) => update({ description: event.target.value })} maxLength={600} rows={5} placeholder="What the project was and why it mattered" /><small>{form.description.length.toLocaleString()} / 600</small></label>
    <label className="profile-collection-editor__wide"><span>Highlights</span><textarea data-profile-collection-field="highlights" value={form.highlights} onChange={(event) => update({ highlights: event.target.value })} maxLength={1807} rows={7} placeholder={"One accomplishment per line\nBuilt…\nImproved…"} /><small>{normalizedLines(form.highlights).length} / 8 highlights · one per line</small></label>
    <label className="profile-collection-editor__wide"><span>Keywords</span><textarea data-profile-collection-field="keywords" value={form.keywords} onChange={(event) => update({ keywords: event.target.value })} maxLength={1215} rows={5} placeholder={"One skill or technology per line"} /><small>{normalizedLines(form.keywords).length} / 16 keywords · one per line</small></label>
  </div>;
}

function EducationFields({ form, setForm, firstInputRef, errorId }: FormProps) {
  if (form.section !== "education") return null;
  const update = (patch: Partial<EducationForm>) => setForm({ ...form, ...patch });
  return <div className="profile-collection-editor__grid">
    <label className="profile-collection-editor__wide"><span>Institution <b aria-hidden="true">*</b></span><input ref={firstInputRef} data-profile-collection-field="institution" value={form.institution} onChange={(event) => update({ institution: event.target.value })} maxLength={240} autoComplete="organization" required aria-describedby={errorId} /></label>
    <label><span>Credential or degree <small>(credential or area required)</small></span><input data-profile-collection-field="studyType" value={form.studyType} onChange={(event) => update({ studyType: event.target.value })} maxLength={160} placeholder="B.S., Certificate, Ph.D." aria-describedby={errorId} /></label>
    <label><span>Area of study <small>(credential or area required)</small></span><input value={form.area} onChange={(event) => update({ area: event.target.value })} maxLength={200} aria-describedby={errorId} /></label>
    <label><span>Start date</span><input value={form.startDate} onChange={(event) => update({ startDate: event.target.value })} maxLength={80} placeholder="2018 or Fall 2018" /></label>
    <label><span>End date</span><input value={form.endDate} onChange={(event) => update({ endDate: event.target.value })} maxLength={80} placeholder="2022 or Expected 2027" /></label>
    <label><span>Score or distinction</span><input value={form.score} onChange={(event) => update({ score: event.target.value })} maxLength={80} placeholder="3.8 GPA or summa cum laude" /></label>
    <label><span>Institution URL</span><input data-profile-collection-field="url" type="url" value={form.url} onChange={(event) => update({ url: event.target.value })} maxLength={2048} placeholder="https://example.edu" aria-describedby={errorId} /></label>
    <label className="profile-collection-editor__wide"><span>Notable courses</span><textarea data-profile-collection-field="courses" value={form.courses} onChange={(event) => update({ courses: event.target.value })} maxLength={1211} rows={6} placeholder="One course per line" /><small>{normalizedLines(form.courses).length} / 12 courses · one per line</small></label>
  </div>;
}

function SkillFields({ form, setForm, firstInputRef, errorId }: FormProps) {
  if (form.section !== "skills") return null;
  const update = (patch: Partial<SkillForm>) => setForm({ ...form, ...patch });
  return <div className="profile-collection-editor__grid">
    <label><span>Skill group <b aria-hidden="true">*</b></span><input ref={firstInputRef} data-profile-collection-field="name" value={form.name} onChange={(event) => update({ name: event.target.value })} maxLength={120} placeholder="Data, Leadership, Tools" required aria-describedby={errorId} /></label>
    <label><span>Level</span><input value={form.level} onChange={(event) => update({ level: event.target.value })} maxLength={80} placeholder="Advanced, fluent, expert" /></label>
    <label className="profile-collection-editor__wide"><span>Skills <b aria-hidden="true">*</b></span><textarea data-profile-collection-field="keywords" value={form.keywords} onChange={(event) => update({ keywords: event.target.value })} maxLength={1217} rows={9} placeholder="One skill per line" aria-describedby={errorId} required /><small>{normalizedLines(form.keywords).length} / 18 skills · one per line</small></label>
  </div>;
}

export function ProfileCollectionFields(props: FormProps) {
  return <>
    <ProjectFields {...props} />
    <EducationFields {...props} />
    <SkillFields {...props} />
  </>;
}

export function ProfileCollectionEditor({
  form,
  setForm,
  original,
  versionNumber,
  saving,
  dirtyCount,
  error,
  conflict,
  requiresProfileName,
  committedVersion,
  headingRef,
  firstInputRef,
  onSubmit,
  onCancel,
  onReloadCurrent,
  onEditBasicDetails,
}: {
  form: ProfileCollectionForm;
  setForm: (next: ProfileCollectionForm) => void;
  original: ProfileCollectionEntry | null;
  versionNumber: number;
  saving: boolean;
  dirtyCount: number;
  error: string | null;
  conflict: boolean;
  requiresProfileName: boolean;
  committedVersion: number | null;
  headingRef: RefObject<HTMLHeadingElement | null>;
  firstInputRef: RefObject<HTMLInputElement | null>;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onCancel: () => void;
  onReloadCurrent: () => void;
  onEditBasicDetails: () => void;
}) {
  const copy = SECTION_COPY[form.section];
  const errorId = error ? "profile-collection-editor-error" : undefined;
  return <article className="profile-review-document profile-collection-editor" aria-labelledby="profile-collection-editor-title">
    <header className="profile-review-section-header"><div><p className="section-kicker">Current profile · Version {versionNumber}</p><h3 id="profile-collection-editor-title" ref={headingRef} tabIndex={-1}>{original ? `Edit ${copy.singular}` : copy.addLabel}</h3><p>{original ? "Queue this edit with the rest of your review; the saved profile does not change until Apply." : "Saving this new entry creates an immutable version and preserves fields this editor does not expose."}</p></div></header>
    <form onSubmit={onSubmit} aria-describedby={errorId}>
      <fieldset disabled={saving || committedVersion !== null}>
        <ProfileCollectionFields form={form} setForm={setForm} firstInputRef={firstInputRef} errorId={errorId} />
      </fieldset>
      {error && <div id="profile-collection-editor-error" className="profile-review-inline-error" role="alert"><p>{error}</p>{requiresProfileName && <button type="button" className="secondary-action" onClick={onEditBasicDetails} disabled={saving}>Edit Basic Details</button>}{conflict && !requiresProfileName && committedVersion === null && <button type="button" className="secondary-action" onClick={onReloadCurrent} disabled={saving}>Reload and review current</button>}</div>}
      <footer className="profile-basics-editor__actions"><span aria-live="polite">{committedVersion ? `Profile version ${committedVersion} is saved; workspace refresh needed` : dirtyCount > 0 ? original ? `${dirtyCount} ${dirtyCount === 1 ? "field" : "fields"} changed` : `New ${copy.singular} not yet saved` : "No unsaved changes"}</span><div><button type="button" className="quiet-action" onClick={() => { if (!saving) onCancel(); }} aria-disabled={saving}>{committedVersion ? "Close" : "Cancel"}</button><button type="submit" className="primary-action" disabled={saving || (dirtyCount === 0 && committedVersion === null) || (conflict && committedVersion === null)}>{saving ? committedVersion ? "Refreshing workspace…" : "Saving new version…" : committedVersion ? "Retry refresh" : original ? "Queue edit" : `${copy.addLabel} as new version`}</button></div></footer>
    </form>
  </article>;
}
