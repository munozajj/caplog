"use strict";

const box = document.getElementById("result");

fetch("/api/verify")
  .then((res) => res.json())
  .then((report) => {
    box.textContent = "";
    if (report.ok) {
      box.classList.add("ok");
      const title = document.createElement("h2");
      title.textContent = "OK";
      const text = document.createElement("p");
      text.textContent = report.summary || "The chain and the checkpoints match.";
      box.append(title, text);
      return;
    }
    box.classList.add("bad");
    const title = document.createElement("h2");
    title.textContent = "Failed";
    const text = document.createElement("p");
    const first = (report.problems && report.problems[0]) || report.summary || "Verification failed.";
    text.textContent = first;
    box.append(title, text);
  })
  .catch((err) => {
    box.classList.add("bad");
    box.textContent = "Failed: " + err.message;
  });
