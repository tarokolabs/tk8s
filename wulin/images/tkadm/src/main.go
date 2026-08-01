package main

import (
  "fmt"
  "net/http"
  "net/http/cgi"
)

func cgiMyDB(w http.ResponseWriter, r *http.Request) {
handler := cgi.Handler{Path: "/opt/www/cgi/mydb.sh"}
handler.ServeHTTP(w, r)
}

func cgiMyData(w http.ResponseWriter, r *http.Request) {
handler := cgi.Handler{Path: "/opt/www/cgi/mydata.sh"}
handler.ServeHTTP(w, r)
}

func cgiInfo(w http.ResponseWriter, r *http.Request) {
handler := cgi.Handler{Path: "/opt/www/cgi/myinfo.sh"}
handler.ServeHTTP(w, r)
}

func main() {
  static := http.FileServer(http.Dir("/opt/www"))
  http.Handle("/",static)

  http.HandleFunc("/db", cgiMyDB)
  http.HandleFunc("/data", cgiMyData)
  http.HandleFunc("/info", cgiInfo)
  fmt.Println(":9090")
  http.ListenAndServe(":9090", nil)
}
