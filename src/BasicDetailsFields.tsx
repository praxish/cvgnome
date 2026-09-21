// SPDX-License-Identifier: MPL-2.0
import type { Ref } from "react";

export type BasicDetailsField =
  | "name"
  | "headline"
  | "summary"
  | "email"
  | "phone"
  | "url"
  | "location.city"
  | "location.region"
  | "location.country_code";

export type BasicDetailsForm = {
  name: string;
  headline: string;
  summary: string;
  email: string;
  phone: string;
  url: string;
  city: string;
  region: string;
  countryCode: string;
};

export type NewProfileBasicDetailsPatch = {
  name: string;
  headline?: string;
  summary?: string;
  email?: string;
  phone?: string;
  url?: string;
  location?: {
    city?: string;
    region?: string;
    country_code?: string;
  };
};

export type BasicDetailsValidationError = {
  field: "name" | "countryCode";
  message: string;
};

export function emptyBasicDetailsForm(): BasicDetailsForm {
  return {
    name: "",
    headline: "",
    summary: "",
    email: "",
    phone: "",
    url: "",
    city: "",
    region: "",
    countryCode: "",
  };
}

export function basicDetailsFormHasInput(form: BasicDetailsForm): boolean {
  return Object.values(form).some((value) => value.trim().length > 0);
}

export function validateBasicDetailsForm(form: BasicDetailsForm): BasicDetailsValidationError | null {
  if (!form.name.trim()) return { field: "name", message: "Name is required." };
  const countryCode = form.countryCode.trim();
  if (countryCode && !/^[A-Z]{2}$/.test(countryCode)) {
    return {
      field: "countryCode",
      message: "Country code must be two uppercase letters, such as US or CA.",
    };
  }
  return null;
}

function normalizedBasicDetailsValue(value: string, summary = false): string {
  let normalized = value.normalize("NFC").replace(/\u00a0/g, " ");
  if (summary) {
    normalized = normalized.replace(/\r\n/g, "\n").replace(/\r/g, "\n").replace(/\t/g, " ");
  }
  return normalized.trim();
}

export function buildNewProfileBasicDetailsPatch(
  form: BasicDetailsForm,
): NewProfileBasicDetailsPatch {
  const patch: NewProfileBasicDetailsPatch = {
    name: normalizedBasicDetailsValue(form.name),
  };
  const optionalFields: Array<keyof Pick<
    BasicDetailsForm,
    "headline" | "summary" | "email" | "phone" | "url"
  >> = ["headline", "summary", "email", "phone", "url"];
  optionalFields.forEach((field) => {
    const value = normalizedBasicDetailsValue(form[field], field === "summary");
    if (value) patch[field] = value;
  });

  const location: NonNullable<NewProfileBasicDetailsPatch["location"]> = {};
  const city = normalizedBasicDetailsValue(form.city);
  const region = normalizedBasicDetailsValue(form.region);
  const countryCode = normalizedBasicDetailsValue(form.countryCode).toUpperCase();
  if (city) location.city = city;
  if (region) location.region = region;
  if (countryCode) location.country_code = countryCode;
  if (Object.keys(location).length > 0) patch.location = location;
  return patch;
}

export function basicDetailsPatchFields(
  patch: NewProfileBasicDetailsPatch,
): BasicDetailsField[] {
  const fields: BasicDetailsField[] = ["name"];
  if (patch.headline !== undefined) fields.push("headline");
  if (patch.summary !== undefined) fields.push("summary");
  if (patch.email !== undefined) fields.push("email");
  if (patch.phone !== undefined) fields.push("phone");
  if (patch.url !== undefined) fields.push("url");
  if (patch.location?.city !== undefined) fields.push("location.city");
  if (patch.location?.region !== undefined) fields.push("location.region");
  if (patch.location?.country_code !== undefined) fields.push("location.country_code");
  return fields;
}

type BasicDetailsFieldsProps = {
  value: BasicDetailsForm;
  onChange: (field: keyof BasicDetailsForm, value: string) => void;
  nameInputRef?: Ref<HTMLInputElement>;
  countryCodeHelpId: string;
};

export function BasicDetailsFields({
  value,
  onChange,
  nameInputRef,
  countryCodeHelpId,
}: BasicDetailsFieldsProps) {
  return (
    <div className="profile-basics-editor__grid">
      <label className="profile-basics-editor__wide">
        <span>Name <b aria-hidden="true">*</b></span>
        <input
          ref={nameInputRef}
          value={value.name}
          onChange={(event) => onChange("name", event.target.value)}
          maxLength={160}
          autoComplete="name"
          required
        />
      </label>
      <label className="profile-basics-editor__wide">
        <span>Headline</span>
        <input
          value={value.headline}
          onChange={(event) => onChange("headline", event.target.value)}
          maxLength={200}
          placeholder="What you do, in one line"
        />
      </label>
      <label className="profile-basics-editor__wide">
        <span>Summary</span>
        <textarea
          value={value.summary}
          onChange={(event) => onChange("summary", event.target.value)}
          maxLength={4000}
          rows={7}
          placeholder="A concise professional summary"
        />
        <small>{value.summary.length.toLocaleString()} / 4,000</small>
      </label>
      <label>
        <span>Email</span>
        <input
          type="email"
          value={value.email}
          onChange={(event) => onChange("email", event.target.value)}
          maxLength={320}
          autoComplete="email"
        />
      </label>
      <label>
        <span>Phone</span>
        <input
          type="tel"
          value={value.phone}
          onChange={(event) => onChange("phone", event.target.value)}
          maxLength={80}
          autoComplete="tel"
        />
      </label>
      <label className="profile-basics-editor__wide">
        <span>Website</span>
        <input
          type="url"
          value={value.url}
          onChange={(event) => onChange("url", event.target.value)}
          maxLength={2048}
          placeholder="https://example.com"
          autoComplete="url"
        />
      </label>
      <label>
        <span>City</span>
        <input
          value={value.city}
          onChange={(event) => onChange("city", event.target.value)}
          maxLength={160}
          autoComplete="address-level2"
        />
      </label>
      <label>
        <span>Region or state</span>
        <input
          value={value.region}
          onChange={(event) => onChange("region", event.target.value)}
          maxLength={160}
          autoComplete="address-level1"
        />
      </label>
      <label>
        <span>Country code</span>
        <input
          value={value.countryCode}
          onChange={(event) => onChange(
            "countryCode",
            event.target.value.toUpperCase().replace(/[^A-Z]/g, "").slice(0, 2),
          )}
          minLength={2}
          maxLength={2}
          pattern="[A-Z]{2}"
          placeholder="US"
          autoComplete="country"
          aria-describedby={countryCodeHelpId}
        />
        <small id={countryCodeHelpId}>Two-letter code; leave blank if unknown.</small>
      </label>
    </div>
  );
}
