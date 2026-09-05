import { describe, expect, it } from "vitest";

import {
  PROVIDER_HELP,
  detectProvider,
  parseGitRemote,
  prNoun,
  providerHost,
  providerLabel,
  resolveProvider,
} from "./gitProviders";

describe("parseGitRemote", () => {
  it("parses https urls and strips .git and trailing slashes", () => {
    expect(parseGitRemote("https://GitHub.com/Example/Demo.git/")).toEqual({
      scheme: "https",
      host: "github.com",
      path: "Example/Demo",
    });
  });

  it("parses scp-like and ssh:// urls", () => {
    expect(parseGitRemote("git@gitlab.com:group/sub/demo.git")).toEqual({
      scheme: "ssh",
      host: "gitlab.com",
      path: "group/sub/demo",
    });
    expect(parseGitRemote("ssh://git@git.example.com:2222/team/demo")).toEqual({
      scheme: "ssh",
      host: "git.example.com",
      path: "team/demo",
    });
  });

  it("returns null for garbage, unsupported schemes and missing paths", () => {
    expect(parseGitRemote("")).toBeNull();
    expect(parseGitRemote("not a url")).toBeNull();
    expect(parseGitRemote("http://github.com/a/b")).toBeNull();
    expect(parseGitRemote("https://github.com/")).toBeNull();
  });
});

describe("detectProvider", () => {
  it.each([
    ["https://github.com/example/demo", "github"],
    ["https://www.github.com/example/demo", "github"],
    ["git@gitlab.com:team/demo.git", "gitlab"],
    ["https://bitbucket.org/team/demo", "bitbucket"],
    ["https://git.example.com/team/demo", "generic"],
    ["garbage", "generic"],
  ])("detects %s as %s", (url, expected) => {
    expect(detectProvider(url)).toBe(expected);
  });

  it("lets an explicit choice override detection", () => {
    expect(resolveProvider("https://gitlab.acme.dev/t/d", "gitlab")).toBe("gitlab");
    expect(resolveProvider("https://gitlab.acme.dev/t/d", "auto")).toBe("generic");
  });
});

describe("labels", () => {
  it("names providers, hosts and the PR noun", () => {
    expect(providerLabel("github")).toBe("GitHub");
    expect(providerLabel("generic")).toBe("Git host");
    expect(providerHost("generic", "ssh://git@git.example.com/t/d")).toBe("git.example.com");
    expect(providerHost("gitlab", "https://gitlab.acme.dev/t/d")).toBe("gitlab.acme.dev");
    expect(providerHost("github")).toBe("github.com");
    expect(prNoun("gitlab")).toBe("merge request");
    expect(prNoun("github")).toBe("pull request");
  });

  it("only requires a username for Bitbucket and keeps the GitHub PAT wording", () => {
    expect(PROVIDER_HELP.bitbucket.requiresUsername).toBe(true);
    expect(PROVIDER_HELP.github.requiresUsername).toBe(false);
    expect(PROVIDER_HELP.github.tokenLabel).toBe("Personal access token");
    expect(PROVIDER_HELP.gitlab.tokenLabel).toBe("Access token");
  });
});
