# Security

HunterAI runs commands, drives a browser, and talks to models on your behalf. A flaw in it
can cost its user a machine or a bounty account, so reports are welcome.

## Reporting a vulnerability

Please do not open a public issue for a vulnerability. Use GitHub's private reporting on
this repository (Security → Report a vulnerability). Say what you did, what happened, and
what you expected; a request or a short script that shows it is worth more than a
description. You will get an answer, and credit in the fix if you want it.

## What counts

- A way for content the model reads (a web page, a file, a tool result, an MCP server's
  tool description, a knowledge-base document) to make HunterAI run something, send
  something, or approve something the owner did not agree to.
- A way around the permission questions, the target scope, or the checks on local
  addresses (the browser proxy, `web_fetch`, Caido's way out).
- A way for another web page or another program on the machine to use HunterAI's API.
- A secret (a key, a header value, a webhook address) shown, logged, or sent where it
  should not be.
- A flaw in the MCP adder or checker that lets a package run code before the owner approved it.

## What does not

- What the owner approved. An approved `execute` command runs with the owner's rights;
  that is what the question is for.
- A model saying something wrong. The answer check and the verification of subagents
  reduce that; they do not end it.
- Findings against third-party tools you put in `bin/` or add as MCP servers: report those
  to their authors.

## Using HunterAI

It is a tool for authorized security testing. Test only what you own or have written
permission to test, inside the scope you were given. You are responsible for what you run
with it.
