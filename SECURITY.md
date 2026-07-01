# Security Policy

## Supported Versions

We actively support security updates for the current version of this project. Please ensure you're using the latest version.

## Reporting a Vulnerability

If you discover a security vulnerability, please **do not** open a public issue. Instead, please report it using GitHub Security Advisories:

1. Navigate to the [Security tab](https://github.com/HH-MWB/aws-glue-toolkit/security) of this repository
2. Click on "Advisories"
3. Click "Report a vulnerability" to create a private security advisory

Alternatively, you can directly access the [Report a vulnerability](https://github.com/HH-MWB/aws-glue-toolkit/security/advisories/new) page.

Please include the following information in your report:

- Description of the vulnerability
- Steps to reproduce the issue
- Potential impact
- Suggested fix (if any)

We will acknowledge receipt of your report within 48 hours and provide an update on the status of the vulnerability within 7 days.

## Scope

`gtk run` and `gtk test` execute user-supplied code inside Docker containers with the job directory mounted on the host. Reports about container escape, mount abuse, or other issues that could compromise the host belong here. Ordinary pip resolution failures, dependency conflicts, or job script errors are not security vulnerabilities — please open a regular [issue](https://github.com/HH-MWB/aws-glue-toolkit/issues/new) for those.

## Disclosure Policy

- We will acknowledge receipt of your vulnerability report
- We will confirm the issue and assess its severity
- We will work on a fix and keep you informed of progress
- Once a fix is ready, we will release it and credit you (unless you prefer to remain anonymous)
