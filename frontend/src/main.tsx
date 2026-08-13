import React from "react";
import ReactDOM from "react-dom/client";
import { createHashRouter, Navigate, RouterProvider } from "react-router-dom";
import Layout from "./components/Layout";
import Setup from "./pages/Setup";
import Prompts from "./pages/Prompts";
import Runner from "./pages/Runner";
import Results from "./pages/Results";
import "./index.css";

const router = createHashRouter([
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <Navigate to="/setup" replace /> },
      { path: "setup", element: <Setup /> },
      { path: "prompts", element: <Prompts /> },
      { path: "run", element: <Runner /> },
      { path: "results", element: <Results /> },
      // Back-compat redirects from the old multi-page structure.
      { path: "auth", element: <Navigate to="/setup" replace /> },
      { path: "runner", element: <Navigate to="/run" replace /> },
      { path: "metrics", element: <Results /> },
      { path: "leaderboard", element: <Results /> },
      { path: "reports", element: <Results /> },
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
);
