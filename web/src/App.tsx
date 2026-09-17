import { useMemo } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { createApi } from "./api";
import { Layout } from "./components/Layout";
import { ApiContext } from "./lib/useApi";
import { HistoryPage } from "./pages/HistoryPage";
import { ReceiptPage } from "./pages/ReceiptPage";
import { TaskPage } from "./pages/TaskPage";

export function App() {
  const api = useMemo(() => createApi(), []);
  const source = import.meta.env.VITE_OPENSHARD_API_URL ? "Hosted" : "Fixture data";
  return (
    <ApiContext.Provider value={api}>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout sourceLabel={source} />}>
            <Route index element={<HistoryPage />} />
            <Route path="tasks/:taskId" element={<TaskPage />} />
            <Route path="receipts/:receiptId" element={<ReceiptPage />} />
            <Route path="*" element={<div className="state">Nothing here. Go back to recent work.</div>} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ApiContext.Provider>
  );
}
