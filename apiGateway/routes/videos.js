const express = require("express");
const axios = require("axios");
const auth = require("../auth/auth");
const router = express.Router({ mergeParams: true });

router.use(auth.checkToken);
router.use(async (req, res) => {
  try {
    const multipart = (req.headers["content-type"] || "").startsWith("multipart/");
    const tail = req.url.replace(/^\/(?=\?|$)/, "");
    // Stream large uploads and downloads rather than buffering a whole video in the gateway.
    const response = await axios({
      url: `http://video_service:8100/${encodeURIComponent(req.params.user)}/${encodeURIComponent(req.params.project)}/videos${tail}`,
      method: req.method,
      headers: { Authorization: req.headers.authorization,
        "Content-Type": req.headers["content-type"] || "application/json",
        ...(req.headers.range ? { Range: req.headers.range } : {}) },
      data: multipart ? req : req.body,
      responseType: "stream", validateStatus: () => true,
      maxBodyLength: 110 * 1024 * 1024, timeout: 180000,
    });
    res.status(response.status);
    for (const name of ["content-type", "content-length", "content-disposition", "content-range", "accept-ranges"]) {
      if (response.headers[name]) res.set(name, response.headers[name]);
    }
    response.data.on("error", () => res.destroy());
    res.on("close", () => response.data.destroy());
    response.data.pipe(res);
  } catch (error) {
    if (!res.headersSent) res.status(503).json({ detail: "Serviço de vídeo indisponível; tente novamente." });
  }
});
module.exports = router;
