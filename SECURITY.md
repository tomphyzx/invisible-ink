# Reporting a bypass

Invisible Ink is a security tool, so a way to hide text from it is a security issue. If you find
one (a document whose hidden text `scan` does not report, or which survives `clean`), please
report it privately first rather than in a public issue:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Describe the trick. A small synthetic document that shows it helps most. Please don't send
   documents that contain anyone's personal data.

We aim to reply within a week, and to credit you in the release that fixes it unless you would
rather not be named.

False alarms (ordinary text reported as hidden) are not security issues. Open a normal issue
for those.

## Scope

In scope: hidden text that `scan` misses, content that survives `clean`, and anything that lets a
document escape the sandbox or affect the computer running Invisible Ink.

Out of scope: text that is visible to a reader (such as words inside a photo), and the limits
listed in the README.
