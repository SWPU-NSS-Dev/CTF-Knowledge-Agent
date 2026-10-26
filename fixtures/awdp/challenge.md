# AWDP 2025 Web Cache Challenge

The challenge awards a flag when a participant can make the preview service read a file outside its workspace. The preview endpoint accepts a URL path and follows redirects before checking the final resolved path.

Writeup North observed that encoded dot segments survive the first normalization pass and reach the file reader. It links this path traversal attack to Fix A: canonicalize once, resolve symlinks, and enforce a workspace-root boundary after resolution.

Writeup South reproduced traversal with a redirect to an internal file URL. It links this path traversal attack to Fix B: disable redirects for previews and allow only workspace-relative paths. This blocks the demonstrated chain but may break legitimate remote previews.

The root cause is that authorization checks the unnormalized input while the file reader uses a normalized destination. A path traversal attack can read `/etc/hostname`; the exploit requires preview access and redirect following.

Fix A resolves the final path and rejects any destination outside the workspace. Its tradeoff is compatibility with existing symlink-based content. Patch A adds a post-resolution root-containment check. Verification runs encoded traversal and symlink escape cases and confirms both are denied.

Fix B disables redirects and restricts previews to workspace-relative paths. It has a smaller code change but removes remote preview functionality. Patch B rejects redirect responses and absolute paths. Verification confirms a normal workspace file still previews while redirect and traversal attempts fail.
